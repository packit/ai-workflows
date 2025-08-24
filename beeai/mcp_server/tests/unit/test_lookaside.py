import asyncio
from pathlib import Path

import pytest
from flexmock import flexmock

import lookaside_tools
from lookaside_tools import download_sources, upload_sources


@pytest.mark.parametrize(
    "internal", [False, True],
)
@pytest.mark.asyncio
async def test_download_sources(internal):
    async def create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "rhpkg" if internal else "centpkg"
        assert args[0] == "sources"
        async def wait():
            return 0
        return flexmock(wait=wait)

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)
    result = await download_sources(dist_git_path=".", internal=internal)
    assert result.startswith("Successfully")


@pytest.mark.parametrize(
    "internal", [False, True],
)
@pytest.mark.asyncio
async def test_upload_sources(internal):
    new_sources = ["package-1.2-3.tar.gz"]

    async def init_kerberos_ticket():
        return True

    async def create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "rhpkg" if internal else "centpkg"
        assert args == ("new-sources", *new_sources)
        async def wait():
            return 0
        return flexmock(wait=wait)

    flexmock(lookaside_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()
    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)
    result = await upload_sources(dist_git_path=".", new_sources=new_sources, internal=internal)
    assert result.startswith("Successfully")
