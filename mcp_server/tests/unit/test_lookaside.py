import asyncio
from pathlib import Path
from textwrap import dedent

import pytest
from flexmock import flexmock

import lookaside_tools
from lookaside_tools import _get_unpacked_sources, download_sources, prep_sources, upload_sources


async def _noop():
    pass


def _mock_kerberos():
    flexmock(lookaside_tools).should_receive("_try_init_kerberos").replace_with(_noop)


@pytest.mark.parametrize(
    "branch", ["c9s", "rhel-9-main"],
)
@pytest.mark.asyncio
async def test_download_sources(branch):
    package = "package"

    async def create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "rhpkg" if branch.startswith("rhel") else "centpkg"
        assert args[3] == "sources"
        async def wait():
            return 0
        return flexmock(wait=wait)

    _mock_kerberos()
    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)
    result = await download_sources(dist_git_path=".", package=package, dist_git_branch=branch)
    assert result.startswith("Successfully")


@pytest.mark.parametrize(
    "branch", ["c9s", "rhel-9-main"],
)
@pytest.mark.asyncio
async def test_prep_sources(branch):
    package = "package"

    async def create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "rhpkg" if branch.startswith("rhel") else "centpkg"
        assert args[3] == "prep"
        async def wait():
            return 0
        return flexmock(wait=wait)

    _mock_kerberos()
    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)
    flexmock(lookaside_tools).should_receive("_get_unpacked_sources").with_args(
        ".", package
    ).and_return("/some/path").once()
    result = await prep_sources(dist_git_path=".", package=package, dist_git_branch=branch)
    assert result == "/some/path"


def test_get_unpacked_sources_default_buildsubdir(tmp_path):
    """Default buildsubdir is %{name}-%{version}."""
    sources_dir = tmp_path / "pkg-1.0-build" / "pkg-1.0"
    sources_dir.mkdir(parents=True)

    spec = tmp_path / "pkg.spec"
    spec.write_text(dedent("""\
        Name: pkg
        Version: 1.0
        Release: 1
        Summary: t
        License: MIT
        %description
        t
        %prep
        %autosetup
    """))
    assert _get_unpacked_sources(tmp_path, "pkg") == str(sources_dir)


def test_get_unpacked_sources_custom_name(tmp_path):
    """Handles %setup -n with a custom directory name."""
    sources_dir = tmp_path / "pkg-1.0-build" / "custom-name"
    sources_dir.mkdir(parents=True)

    spec = tmp_path / "pkg.spec"
    spec.write_text(dedent("""\
        Name: pkg
        Version: 1.0
        Release: 1
        Summary: t
        License: MIT
        %description
        t
        %prep
        %setup -q -n custom-name
    """))
    assert _get_unpacked_sources(tmp_path, "pkg") == str(sources_dir)


def test_get_unpacked_sources_multi_dir_buildsubdir(tmp_path):
    """Handles %setup -n with multi-directory buildsubdir containing macros."""
    sources_dir = tmp_path / "expat-2.6.4-build" / "libexpat-R_2_6_4" / "expat"
    sources_dir.mkdir(parents=True)

    spec = tmp_path / "expat.spec"
    spec.write_text(dedent("""\
        Name: expat
        Version: 2.6.4
        Release: 1
        Summary: t
        License: MIT
        %define unversion %(echo %{version} | tr . _)
        %description
        t
        %prep
        %setup -q -n libexpat-R_%{unversion}/expat
    """))
    assert _get_unpacked_sources(tmp_path, "expat") == str(sources_dir)


@pytest.mark.parametrize(
    "branch", ["c10s", "rhel-10-main"],
)
@pytest.mark.asyncio
async def test_upload_sources(branch):
    package = "package"
    new_sources = ["package-1.2-3.tar.gz"]

    async def init_kerberos_ticket():
        return True

    async def create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "rhpkg" if branch.startswith("rhel") else "centpkg"
        assert args[3:] == ("new-sources", *new_sources)
        async def wait():
            return 0
        return flexmock(wait=wait)

    flexmock(lookaside_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()
    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)
    result = await upload_sources(dist_git_path=".", package=package, dist_git_branch=branch, new_sources=new_sources)
    assert result.startswith("Successfully")
