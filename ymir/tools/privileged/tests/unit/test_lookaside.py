import asyncio
import os

import pytest
from flexmock import flexmock

import lookaside_tools
from lookaside_tools import DownloadSourcesTool, PrepSourcesTool, UploadSourcesTool


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

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)
    result = (
        await DownloadSourcesTool().run(
            input={"dist_git_path": os.getcwd(), "package": package, "dist_git_branch": branch}
        )
    ).result
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
    result = (
        await PrepSourcesTool().run(
            input={"dist_git_path": os.getcwd(), "package": package, "dist_git_branch": branch}
        )
    ).result
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
    result = (
        await UploadSourcesTool().run(
            input={
                "dist_git_path": os.getcwd(),
                "package": package,
                "dist_git_branch": branch,
                "new_sources": new_sources,
            }
        )
    ).result
    assert result.startswith("Successfully")
