import asyncio
from pathlib import Path

import pytest
from flexmock import flexmock

import lookaside_tools
from lookaside_tools import download_sources, upload_sources


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

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)
    result = await download_sources(dist_git_path=".", package=package, dist_git_branch=branch)
    assert result.startswith("Successfully")


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
