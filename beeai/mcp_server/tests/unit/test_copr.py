import asyncio
from pathlib import Path

import pytest
from copr.v3 import ProjectProxy, BuildProxy
from flexmock import flexmock

import copr_tools
from copr_tools import COPR_USER, COPR_PROJECT_LIFETIME, COPR_BUILD_TIMEOUT, build_package


@pytest.mark.parametrize(
    "existing_project",
    [False, True],
)
@pytest.mark.parametrize(
    "build_failure",
    [False, True],
)
@pytest.mark.asyncio
async def test_build_package(existing_project, build_failure):
    srpm_path = "test.src.rpm"
    dist_git_branch = "c10s"
    jira_issue = "RHEL-12345"
    chroot = "rhel-10.dev-x86_64"

    async def init_kerberos_ticket():
        return True

    async def _get_exclusive_arches(*_):
        return set()

    async def sleep(*_):
        # do not waste time
        return

    flexmock(copr_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()
    flexmock(copr_tools).should_receive("_get_exclusive_arches").replace_with(_get_exclusive_arches).once()
    flexmock(asyncio).should_receive("sleep").replace_with(sleep)
    flexmock(ProjectProxy).should_receive("get").with_args(COPR_USER, jira_issue).and_return(
        flexmock() if existing_project else None
    ).once()
    kwargs = {
        "ownername": COPR_USER,
        "projectname": jira_issue,
        "chroots": [chroot],
        "description": f"Test builds for {jira_issue}",
        "delete_after_days": COPR_PROJECT_LIFETIME,
    }
    if existing_project:
        flexmock(ProjectProxy).should_receive("edit").with_args(**kwargs).once()
    else:
        flexmock(ProjectProxy).should_receive("add").with_args(**kwargs).once()
    flexmock(BuildProxy).should_receive("create_from_file").with_args(
        ownername=COPR_USER,
        projectname=jira_issue,
        path=srpm_path,
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
            chroot: {"packages": [{"name": "test", "version": "0.1", "release": "1.el10", "arch": "x86_64"}]}
        }
    ).once()
    result = await build_package(srpm_path=srpm_path, dist_git_branch=dist_git_branch, jira_issue=jira_issue)
    assert result.success == (not build_failure)
    assert any(url.endswith("builder-live.log.gz") for url in result.artifacts_urls)
    assert any(url.endswith("root.log.gz") for url in result.artifacts_urls)
    if not build_failure:
        assert any(url.endswith("test-0.1-1.el10.x86_64.rpm") for url in result.artifacts_urls)
