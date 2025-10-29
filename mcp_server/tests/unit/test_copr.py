import asyncio
import gzip
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from copr.v3 import ProjectProxy, BuildProxy
from fastmcp.exceptions import ToolError
from flexmock import flexmock

import copr_tools
from copr_tools import (
    COPR_PROJECT_LIFETIME,
    COPR_BUILD_TIMEOUT,
    build_package,
    download_artifacts,
)


@pytest.mark.parametrize(
    "build_failure",
    [False, True],
)
@pytest.mark.parametrize(
    "exclusive_arch",
    [None, "ppc64le"],
)
@pytest.mark.parametrize(
    "dist_git_branch",
    ["c10s", "rhel-10.1", "rhel-10.0"],
)
@pytest.mark.asyncio
async def test_build_package(build_failure, exclusive_arch, dist_git_branch):
    ownername = "jotnar-bot"
    srpm_path = Path("test.src.rpm")
    jira_issue = "RHEL-12345"
    suffix = "" if dist_git_branch == "rhel-10.0" else ".dev"
    chroot = f"rhel-10{suffix}-{exclusive_arch or 'x86_64'}"
    existing_chroot = "rhel-9.dev-x86_64"

    async def init_kerberos_ticket():
        return f"{ownername}@EXAMPLE.COM"

    async def load_rhel_config():
        return {
            "current_z_streams": {"10": "rhel-10.0.z"},
            "upcoming_z_streams": {"10": "rhel-10.1.z"},
        }

    async def _get_exclusive_arches(*_):
        return {exclusive_arch} if exclusive_arch else set()

    async def sleep(*_):
        # do not waste time
        return

    flexmock(copr_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()
    flexmock(copr_tools).should_receive("load_rhel_config").replace_with(load_rhel_config).once()
    flexmock(copr_tools).should_receive("_get_exclusive_arches").replace_with(_get_exclusive_arches).once()
    flexmock(asyncio).should_receive("sleep").replace_with(sleep)

    kwargs = {
        "ownername": ownername,
        "projectname": jira_issue,
        "chroots": [chroot],
        "description": f"Test builds for {jira_issue}",
        "delete_after_days": COPR_PROJECT_LIFETIME,
    }
    flexmock(ProjectProxy).should_receive("add").with_args(exist_ok=True, **kwargs).and_return(
        flexmock(chroot_repos={existing_chroot: "http://some.url"})
    ).once()
    kwargs["chroots"] = sorted({existing_chroot} | {chroot})
    flexmock(ProjectProxy).should_receive("edit").with_args(**kwargs).once()
    flexmock(BuildProxy).should_receive("create_from_file").with_args(
        ownername=ownername,
        projectname=jira_issue,
        path=str(srpm_path),
        buildopts={"chroots": [chroot], "timeout": COPR_BUILD_TIMEOUT},
    ).and_return(flexmock(id=12345)).once()
    flexmock(BuildProxy).should_receive("get").with_args(12345).and_return(
        flexmock(state="running", id=12345)
    ).and_return(
        flexmock(
            state="failed" if build_failure else "succeeded",
            source_package={"name": "test"},
            repo_url="http://some.url",
            id=12345,
        )
    ).twice()
    flexmock(BuildProxy).should_receive("get_built_packages").with_args(12345).and_return(
        {}
        if build_failure
        else {
            chroot: {
                "packages": [
                    {
                        "name": "test",
                        "version": "0.1",
                        "release": "1.el10",
                        "arch": exclusive_arch or "x86_64",
                    }
                ]
            }
        }
    ).once()
    result = await build_package(srpm_path=srpm_path, dist_git_branch=dist_git_branch, jira_issue=jira_issue)
    assert result.success == (not build_failure)
    assert any(url.endswith("builder-live.log.gz") for url in result.artifacts_urls)
    assert any(url.endswith("root.log.gz") for url in result.artifacts_urls)
    if not build_failure:
        assert any(
            url.endswith(f"test-0.1-1.el10.{exclusive_arch or 'x86_64'}.rpm") for url in result.artifacts_urls
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://test.url/builder-live.log.gz",
        "https://test.url/build.log.gz",
        "https://broken.url/root.log.gz",
        "https://test.url/test-0.1-1.el10.rpm",
    ],
)
@pytest.mark.asyncio
async def test_download_artifacts(url, tmp_path):
    artifacts_urls = [url]
    target_path = tmp_path
    content = b"12345"
    content_gz = b"\x1f\x8b\x00\x00"

    @asynccontextmanager
    async def get(url):
        async def read():
            return content_gz if url.endswith(".log.gz") and not url.endswith("build.log.gz") else content
        yield flexmock(status=404 if "broken" in url else 200, reason="Because", read=read)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)
    flexmock(gzip).should_receive("decompress").and_return(content).times(
        1 if url.endswith(".log.gz") and not url.endswith("build.log.gz") and "broken" not in url else 0
    )
    if "broken" in url:
        with pytest.raises(ToolError):
            await download_artifacts(artifacts_urls=artifacts_urls, target_path=target_path)
    else:
        result = await download_artifacts(artifacts_urls=artifacts_urls, target_path=target_path)
        assert result.startswith("Successfully")
    path = target_path / url.rsplit("/", 1)[-1].removesuffix(".gz")
    if "broken" in url:
        assert not path.is_file()
    else:
        assert path.read_bytes() == content
