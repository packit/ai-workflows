import asyncio
from pathlib import Path

import gitlab
from ogr.abstract import PRStatus
from ogr.exceptions import GitlabAPIException
import pytest
from flexmock import flexmock
from ogr.services.gitlab import GitlabService

from gitlab_tools import fork_repository, open_merge_request, push_to_remote_repository


@pytest.mark.asyncio
async def test_fork_repository():
    repository = "https://gitlab.com/redhat/centos-stream/rpms/bash"
    clone_url = "https://gitlab.com/ai-bot/bash.git"
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=repository
    ).and_return(
        flexmock(get_fork=lambda create: flexmock(get_git_urls=lambda: {"git": clone_url}))
    )
    assert await fork_repository(repository=repository) == clone_url


@pytest.mark.asyncio
async def test_open_merge_request():
    fork_url = "https://gitlab.com/ai-bot/bash.git"
    title = "Fix RHEL-12345"
    description = "Resolves RHEL-12345"
    target = "c10s"
    source = "automated-package-update-RHEL-12345"
    mr_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/1"
    pr_mock = flexmock(url=mr_url, status=PRStatus.open, id=1)
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=fork_url
    ).and_return(flexmock(create_pr=lambda title, body, target, source: pr_mock, get_pr=lambda id: pr_mock))
    pr_mock.should_receive("add_label").with_args("jotnar_needs_attention").once()
    assert (
        await open_merge_request(
            fork_url=fork_url,
            title=title,
            description=description,
            target=target,
            source=source,
        )
        == mr_url
    )


@pytest.mark.asyncio
async def test_open_merge_request_with_existing_mr():
    fork_url = "https://gitlab.com/ai-bot/bash.git"
    title = "Fix RHEL-12345"
    description = "Resolves RHEL-12345"
    target = "c10s"
    source = "automated-package-update-RHEL-12345"
    mr_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/1"
    pr_mock = flexmock(url=mr_url, source_branch=source, status=PRStatus.open, target_branch=target, id=1)

    # create_pr raises an exception with code 409 indicating the MR already exists
    def create_pr_raises(*args, **kwargs):
        exc = GitlabAPIException()
        exc.__cause__ = gitlab.GitlabError(response_code=409)
        raise exc

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=fork_url
    ).and_return(
        flexmock(
            create_pr=create_pr_raises,
            parent=flexmock(get_pr_list=lambda: [pr_mock], get_pr=lambda id: pr_mock),
            get_pr=lambda id: pr_mock,
        )
    )
    pr_mock.should_receive("add_label").with_args("jotnar_needs_attention").once()
    assert (
        await open_merge_request(
            fork_url=fork_url,
            title=title,
            description=description,
            target=target,
            source=source,
        )
        == mr_url
    )


@pytest.mark.asyncio
async def test_push_to_remote_repository():
    repository = "https://gitlab.com/ai-bot/bash.git"
    branch = "automated-package-update-RHEL-12345"
    clone_path = Path("/git-repos/bash")

    async def create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "git"
        assert args[0] == "push"
        assert args[1].endswith(repository.removeprefix("https://"))
        assert args[2] == branch
        assert kwargs.get("cwd") == clone_path
        async def wait():
            return 0
        return flexmock(wait=wait)

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)
    result = await push_to_remote_repository(repository=repository, clone_path=clone_path, branch=branch)
    assert result.startswith("Successfully")
