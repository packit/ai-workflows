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
def release_macro_spec(tmp_path):
    spec = tmp_path / "release_macro.spec"
    spec.write_text(
        dedent(
            """\
            %global my_release 5%{?dist}

            Name:           test
            Version:        0.1
            Release:        %{my_release}
            Summary:        Test package

            License:        MIT

            %description
            Test package

            %changelog
            * Thu Jun 07 2018 Nikola Forró <nforro@redhat.com> - 0.1-5
            - first version
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


@pytest.fixture
def spec_with_autosetup_gendiff(tmp_path):
    spec = tmp_path / "gendiff.spec"
    source_file = tmp_path / "source.tar.gz"
    source_file.touch()
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        5.0.0
            Release:        1%{?dist}
            Summary:        Test package

            License:        MIT

            Source0:        source.tar.gz
            Patch0:         fix-one.patch
            Patch1:         fix-two.patch

            %description
            Test package with autosetup gendiff

            %prep
            %autosetup -p1 -S gendiff

            %changelog
            * Thu Jan 13 3770 Test User <test@redhat.com> - 5.0.0-1
            - first version
            """
        )
    )
    return spec


@pytest.fixture
def spec_with_autosetup_no_p(tmp_path):
    spec = tmp_path / "no_p.spec"
    source_file = tmp_path / "source.tar.gz"
    source_file.touch()
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        6.0.0
            Release:        1%{?dist}
            Summary:        Test package

            License:        MIT

            Source0:        source.tar.gz
            Patch0:         only-fix.patch

            %description
            Test package with bare autosetup (no -p flag)

            %prep
            %autosetup

            %changelog
            * Thu Jan 13 3770 Test User <test@redhat.com> - 6.0.0-1
            - first version
            """
        )
    )
    return spec


@pytest.fixture
def spec_with_individual_patch_strips(tmp_path):
    spec = tmp_path / "individual_strips.spec"
    source_file = tmp_path / "source.tar.gz"
    source_file.touch()
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        2.0.0
            Release:        1%{?dist}
            Summary:        Test package

            License:        MIT

            Source0:        source.tar.gz
            Patch0:         fix-p0.patch
            Patch1:         fix-p2.patch
            Patch2:         fix-default.patch

            %description
            Test package with individual patch macros using different strip levels

            %prep
            %setup -q
            %patch 0 -p0
            %patch 1 -p2
            %patch 2 -p1

            %changelog
            * Thu Jan 13 3770 Test User <test@redhat.com> - 2.0.0-1
            - first version
            """
        )
    )
    return spec


@pytest.fixture
def spec_with_autopatch(tmp_path):
    spec = tmp_path / "autopatch.spec"
    source_file = tmp_path / "source.tar.gz"
    source_file.touch()
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        4.1.0
            Release:        1%{?dist}
            Summary:        Test package

            License:        MIT

            Source0:        source.tar.gz
            Patch0:         backport-fix.patch
            Patch1:         memory-fix.patch

            %description
            Test package with autopatch

            %prep
            %setup -q
            %autopatch -p2

            %changelog
            * Thu Jan 13 3770 Test User <test@redhat.com> - 4.1.0-1
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
        "* Tue Aug 05 2025 RHEL Packaging Agent <jotnar@redhat.com> - 0.1-5",
        "- some change",
        "  second line",
        "",
    ]


@pytest.mark.parametrize(
    "spec_fixture, expected_version, expected_patches, expected_strip_levels",
    [
        (
            "spec_with_patches",
            "1.2.3",
            [
                "fix-cve-2024-1234.patch",
                "fix-memory-leak.patch",
                "update-documentation.patch",
            ],
            {
                "fix-cve-2024-1234.patch": 1,
                "fix-memory-leak.patch": 1,
                "update-documentation.patch": 1,
            },
        ),
        (
            "spec_with_macro_patches",
            "3.5.3",
            [
                "mypackage-3.5.3-Fix-CVE-2026-4111.patch",
            ],
            {
                "mypackage-3.5.3-Fix-CVE-2026-4111.patch": 1,
            },
        ),
        ("minimal_spec", "0.1", [], {}),
        (
            "spec_with_individual_patch_strips",
            "2.0.0",
            ["fix-p0.patch", "fix-p2.patch", "fix-default.patch"],
            {
                "fix-p0.patch": 0,
                "fix-p2.patch": 2,
                "fix-default.patch": 1,
            },
        ),
        (
            "spec_with_autopatch",
            "4.1.0",
            ["backport-fix.patch", "memory-fix.patch"],
            {
                "backport-fix.patch": 2,
                "memory-fix.patch": 2,
            },
        ),
        (
            "spec_with_autosetup_gendiff",
            "5.0.0",
            ["fix-one.patch", "fix-two.patch"],
            {
                "fix-one.patch": 1,
                "fix-two.patch": 1,
            },
        ),
        (
            "spec_with_autosetup_no_p",
            "6.0.0",
            ["only-fix.patch"],
            {
                "only-fix.patch": 1,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_get_package_info(
    spec_fixture,
    expected_version,
    expected_patches,
    expected_strip_levels,
    request,
):
    spec = request.getfixturevalue(spec_fixture)
    tool = GetPackageInfoTool()

    output = await tool.run(input=GetPackageInfoToolInput(spec=spec)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.to_json_safe()

    assert result.version == expected_version
    assert result.patch_files == expected_patches
    assert result.patch_strip_levels == expected_strip_levels


@pytest.mark.parametrize(
    "rebase_in_current_stream",
    [False, True],
)
@pytest.mark.parametrize(
    "rebase_in_higher_stream",
    [False, True],
)
@pytest.mark.parametrize(
    "dist_git_branch",
    ["c9s", "c10s", "rhel-9.6.0", "rhel-10.0"],
)
@pytest.mark.asyncio
async def test_update_release(
    rebase_in_current_stream,
    rebase_in_higher_stream,
    dist_git_branch,
    minimal_spec,
    autorelease_spec,
    release_macro_spec,
):
    package = "test"

    async def _get_latest_candidate_build(package, candidate_tag):
        if candidate_tag.startswith(dist_git_branch):
            return EVR(version="0.1", release="5.elX")
        if rebase_in_higher_stream:
            return EVR(version="0.2", release="2.elX")
        return EVR(version="0.1", release="8.elX")

    flexmock(UpdateReleaseTool).should_receive("_get_latest_candidate_build").replace_with(
        _get_latest_candidate_build
    )

    tool = UpdateReleaseTool()

    async def run_and_check(spec, expected_release, error=False):
        with pytest.raises(ToolError) if error else contextlib.nullcontext() as e:
            output = await tool.run(
                input=UpdateReleaseToolInput(
                    spec=spec,
                    package=package,
                    dist_git_branch=dist_git_branch,
                    rebase=rebase_in_current_stream,
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))
        if error:
            return e.value.message
        result = output.result
        assert result.startswith("Successfully")
        release_line = next(line for line in spec.read_text().splitlines() if line.startswith("Release:"))
        assert release_line == f"Release:        {expected_release}"
        return result

    if not dist_git_branch.startswith("rhel-"):
        await run_and_check(minimal_spec, "1%{?dist}" if rebase_in_current_stream else "6%{?dist}")
        await run_and_check(autorelease_spec, "%autorelease")
        await run_and_check(release_macro_spec, "1%{?dist}" if rebase_in_current_stream else "6%{?dist}")
    else:
        await run_and_check(minimal_spec, "0%{?dist}.1" if rebase_in_current_stream else "5%{?dist}.1")
        await run_and_check(
            autorelease_spec,
            "0%{?dist}.%{autorelease -n}"
            if rebase_in_current_stream
            else "5%{?dist}.%{autorelease -n}"
            if rebase_in_higher_stream
            else "8%{?dist}.%{autorelease -n}",
        )
        await run_and_check(
            release_macro_spec,
            "0%{?dist}.1" if rebase_in_current_stream else "5%{?dist}.1",
        )
        await run_and_check(minimal_spec, "0%{?dist}.1" if rebase_in_current_stream else "5%{?dist}.2")
        await run_and_check(
            autorelease_spec,
            "0%{?dist}.%{autorelease -n}"
            if rebase_in_current_stream
            else "5%{?dist}.%{autorelease -n}"
            if rebase_in_higher_stream
            else "8%{?dist}.%{autorelease -n}",
        )
        await run_and_check(
            release_macro_spec,
            "0%{?dist}.1" if rebase_in_current_stream else "5%{?dist}.2",
        )

    with specfile.Specfile(minimal_spec) as spec:
        spec.raw_release = "5%{?dist}.1"
    with specfile.Specfile(autorelease_spec) as spec:
        spec.raw_release = "5%{?dist}.%{autorelease -n}"

    if not dist_git_branch.startswith("rhel-"):
        await run_and_check(minimal_spec, "1%{?dist}" if rebase_in_current_stream else "6%{?dist}")
        await run_and_check(autorelease_spec, "%autorelease")

    with specfile.Specfile(minimal_spec) as spec:
        spec.raw_release = "5%{?alphatag:.%{alphatag}}%{?dist}.8"

    if not dist_git_branch.startswith("rhel-"):
        await run_and_check(
            minimal_spec,
            (
                "1%{?alphatag:.%{alphatag}}%{?dist}"
                if rebase_in_current_stream
                else "6%{?alphatag:.%{alphatag}}%{?dist}"
            ),
        )
    else:
        await run_and_check(
            minimal_spec,
            "0%{?dist}.1" if rebase_in_current_stream else "5%{?alphatag:.%{alphatag}}%{?dist}.9",
        )
        await run_and_check(
            minimal_spec,
            "0%{?dist}.1" if rebase_in_current_stream else "5%{?alphatag:.%{alphatag}}%{?dist}.10",
        )


@pytest.mark.parametrize(
    "rebase",
    [False, True],
)
@pytest.mark.parametrize(
    "rebase_in_higher_stream",
    [False, True],
)
@pytest.mark.asyncio
async def test_update_release_abandon_autorelease(
    rebase,
    rebase_in_higher_stream,
    autorelease_spec,
    minimal_spec,
):
    """Test that abandon_autorelease replaces %autorelease with a numeric counter on Z-stream."""
    package = "test"
    dist_git_branch = "rhel-9.6.0"

    async def _get_latest_candidate_build(package, candidate_tag):
        if candidate_tag.startswith(dist_git_branch):
            return EVR(version="0.1", release="5.elX")
        if rebase_in_higher_stream:
            return EVR(version="0.2", release="2.elX")
        return EVR(version="0.1", release="8.elX")

    flexmock(UpdateReleaseTool).should_receive("_get_latest_candidate_build").replace_with(
        _get_latest_candidate_build
    )

    tool = UpdateReleaseTool()

    async def run_and_check(spec, expected_release):
        output = await tool.run(
            input=UpdateReleaseToolInput(
                spec=spec,
                package=package,
                dist_git_branch=dist_git_branch,
                rebase=rebase,
                abandon_autorelease=True,
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
        result = output.result
        assert result.startswith("Successfully")
        release_line = next(line for line in spec.read_text().splitlines() if line.startswith("Release:"))
        assert release_line == f"Release:        {expected_release}"

    # With %autorelease in the spec, abandon_autorelease should produce numeric release
    base = "0" if rebase else ("5" if rebase_in_higher_stream else "8")
    await run_and_check(autorelease_spec, f"{base}%{{?dist}}.1")

    # Without %autorelease, abandon_autorelease has no special effect — normal z-stream logic applies
    await run_and_check(minimal_spec, "0%{?dist}.1" if rebase else "5%{?dist}.1")


@pytest.mark.parametrize(
    "rebase_in_higher_stream",
    [False, True],
)
@pytest.mark.asyncio
async def test_update_release_abandon_autorelease_increments_zstream(
    rebase_in_higher_stream,
    autorelease_spec,
):
    """Test that abandon_autorelease increments Z-stream suffix from existing builds."""
    package = "test"
    dist_git_branch = "rhel-9.6.0"

    async def _get_latest_candidate_build(package, candidate_tag):
        if candidate_tag.startswith(dist_git_branch):
            return EVR(version="0.1", release="5.elX.3")
        if rebase_in_higher_stream:
            return EVR(version="0.2", release="2.elX")
        return EVR(version="0.1", release="8.elX")

    flexmock(UpdateReleaseTool).should_receive("_get_latest_candidate_build").replace_with(
        _get_latest_candidate_build
    )

    tool = UpdateReleaseTool()
    output = await tool.run(
        input=UpdateReleaseToolInput(
            spec=autorelease_spec,
            package=package,
            dist_git_branch=dist_git_branch,
            rebase=False,
            abandon_autorelease=True,
        )
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    release_line = next(
        line for line in autorelease_spec.read_text().splitlines() if line.startswith("Release:")
    )
    base = "5" if rebase_in_higher_stream else "8"
    assert release_line == f"Release:        {base}%{{?dist}}.4"


@pytest.mark.asyncio
async def test_update_release_abandon_autorelease_non_zstream(autorelease_spec):
    """Test that abandon_autorelease has no effect on non-Z-stream branches."""
    package = "test"
    dist_git_branch = "c10s"

    tool = UpdateReleaseTool()

    output = await tool.run(
        input=UpdateReleaseToolInput(
            spec=autorelease_spec,
            package=package,
            dist_git_branch=dist_git_branch,
            rebase=False,
            abandon_autorelease=True,
        )
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    release_line = next(
        line for line in autorelease_spec.read_text().splitlines() if line.startswith("Release:")
    )
    assert release_line == "Release:        %autorelease"
