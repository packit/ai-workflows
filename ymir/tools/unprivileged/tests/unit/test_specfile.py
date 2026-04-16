import contextlib
import datetime
from textwrap import dedent

import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError
from flexmock import flexmock
from specfile import specfile
from specfile.utils import EVR

from ymir.tools.unprivileged.specfile import (
    AddChangelogEntryTool,
    AddChangelogEntryToolInput,
    GetPackageInfoTool,
    GetPackageInfoToolInput,
    UpdateReleaseTool,
    UpdateReleaseToolInput,
)


@pytest.fixture
def autorelease_spec(tmp_path):
    spec = tmp_path / "autorelease.spec"
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        0.1
            Release:        %autorelease
            Summary:        Test package

            License:        MIT

            %description
            Test package

            %changelog
            %autochangelog
            """
        )
    )
    return spec


@pytest.fixture
def spec_with_patches(tmp_path):
    spec = tmp_path / "with_patches.spec"
    source_file = tmp_path / "source.tar.gz"
    source_file.touch()
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        1.2.3
            Release:        1%{?dist}
            Summary:        Test package

            License:        MIT

            Source0:        source.tar.gz
            Patch0:         fix-cve-2024-1234.patch
            Patch1:         fix-memory-leak.patch
            Patch2:         update-documentation.patch

            %description
            Test package with patches

            %prep
            %autosetup -p1

            %changelog
            * Thu Jan 13 3770 Test User <test@redhat.com> - 1.2.3-1
            - first version
            """
        )
    )
    return spec


@pytest.fixture
def spec_with_macro_patches(tmp_path):
    spec = tmp_path / "macro_patches.spec"
    source_file = tmp_path / "source.tar.gz"
    source_file.touch()
    spec.write_text(
        dedent(
            """
            Name:           mypackage
            Version:        3.5.3
            Release:        1%{?dist}
            Summary:        Test package

            License:        MIT

            Source0:        source.tar.gz
            Patch0:         %{name}-%{version}-Fix-CVE-2026-4111.patch

            %description
            Test package with macro-containing patch names

            %prep
            %autosetup -p1

            %changelog
            * Thu Jan 13 3770 Test User <test@redhat.com> - 3.5.3-1
            - first version
            """
        )
    )
    return spec


@pytest.mark.asyncio
async def test_add_changelog_entry(minimal_spec):
    content = ["- some change", "  second line"]
    flexmock(specfile).should_receive("guess_packager").and_return("RHEL Packaging Agent <jotnar@redhat.com>")
    flexmock(specfile).should_receive("datetime").and_return(
        flexmock(
            datetime=flexmock(now=lambda _: flexmock(date=lambda: datetime.date(2025, 8, 5))),
            timezone=flexmock(utc=None),
        )
    )
    tool = AddChangelogEntryTool()
    output = await tool.run(input=AddChangelogEntryToolInput(spec=minimal_spec, content=content)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result.startswith("Successfully")
    assert minimal_spec.read_text().splitlines()[-7:-2] == [
        "%changelog",
        "* Tue Aug 05 2025 RHEL Packaging Agent <jotnar@redhat.com> - 0.1-2",
        "- some change",
        "  second line",
        "",
    ]


@pytest.mark.parametrize(
    "spec_fixture, expected_version, expected_patches",
    [
        (
            "spec_with_patches",
            "1.2.3",
            [
                "fix-cve-2024-1234.patch",
                "fix-memory-leak.patch",
                "update-documentation.patch",
            ],
        ),
        (
            "spec_with_macro_patches",
            "3.5.3",
            [
                "mypackage-3.5.3-Fix-CVE-2026-4111.patch",
            ],
        ),
        ("minimal_spec", "0.1", []),
    ],
)
@pytest.mark.asyncio
async def test_get_package_info(spec_fixture, expected_version, expected_patches, request):
    spec = request.getfixturevalue(spec_fixture)
    tool = GetPackageInfoTool()

    output = await tool.run(input=GetPackageInfoToolInput(spec=spec)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.to_json_safe()

    assert result.version == expected_version
    assert result.patch_files == expected_patches


@pytest.mark.parametrize(
    "rebase",
    [False, True],
)
@pytest.mark.parametrize(
    "dist_git_branch",
    ["c9s", "c10s", "rhel-9.6.0", "rhel-10.0"],
)
@pytest.mark.asyncio
async def test_update_release(rebase, dist_git_branch, minimal_spec, autorelease_spec):
    package = "test"

    async def _get_latest_higher_stream_build(*_, **__):
        return EVR(version="0.1", release="2.elX")

    flexmock(UpdateReleaseTool).should_receive("_get_latest_higher_stream_build").replace_with(
        _get_latest_higher_stream_build
    )

    tool = UpdateReleaseTool()

    async def run_and_check(spec, expected_release, error=False):
        with pytest.raises(ToolError) if error else contextlib.nullcontext() as e:
            output = await tool.run(
                input=UpdateReleaseToolInput(
                    spec=spec,
                    package=package,
                    dist_git_branch=dist_git_branch,
                    rebase=rebase,
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))
        if error:
            return e.value.message
        result = output.result
        assert result.startswith("Successfully")
        assert spec.read_text().splitlines()[3] == f"Release:        {expected_release}"
        return result

    if not dist_git_branch.startswith("rhel-"):
        await run_and_check(minimal_spec, "1%{?dist}" if rebase else "3%{?dist}")
        await run_and_check(autorelease_spec, "%autorelease")
    else:
        await run_and_check(minimal_spec, "0%{?dist}.1" if rebase else "2%{?dist}.1")
        await run_and_check(
            autorelease_spec,
            "0%{?dist}.%{autorelease -n}" if rebase else "2%{?dist}.%{autorelease -n}",
        )
        await run_and_check(minimal_spec, "0%{?dist}.1" if rebase else "2%{?dist}.2")
        await run_and_check(
            autorelease_spec,
            "0%{?dist}.%{autorelease -n}" if rebase else "2%{?dist}.%{autorelease -n}",
        )

    with specfile.Specfile(minimal_spec) as spec:
        spec.raw_release = "2%{?dist}.1"
    with specfile.Specfile(autorelease_spec) as spec:
        spec.raw_release = "2%{?dist}.%{autorelease -n}"

    if not dist_git_branch.startswith("rhel-"):
        await run_and_check(minimal_spec, "1%{?dist}" if rebase else "3%{?dist}")
        await run_and_check(autorelease_spec, "%autorelease")

    with specfile.Specfile(minimal_spec) as spec:
        spec.raw_release = "5%{?alphatag:.%{alphatag}}%{?dist}.8"

    if not dist_git_branch.startswith("rhel-"):
        await run_and_check(
            minimal_spec,
            ("1%{?alphatag:.%{alphatag}}%{?dist}" if rebase else "6%{?alphatag:.%{alphatag}}%{?dist}"),
        )
    else:
        await run_and_check(
            minimal_spec,
            "0%{?dist}.1" if rebase else "5%{?alphatag:.%{alphatag}}%{?dist}.9",
        )
        await run_and_check(
            minimal_spec,
            "0%{?dist}.1" if rebase else "5%{?alphatag:.%{alphatag}}%{?dist}.10",
        )
