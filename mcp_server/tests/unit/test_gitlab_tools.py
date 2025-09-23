import asyncio
from pathlib import Path

import gitlab
from ogr.abstract import PRStatus
from ogr.exceptions import GitlabAPIException
from ogr.services.gitlab.project import GitlabProject
import pytest
from flexmock import flexmock
from ogr.services.gitlab import GitlabService

from gitlab_tools import fork_repository, open_merge_request, push_to_remote_repository, clone_repository


@pytest.mark.parametrize(
    "repository",
    [
        "https://gitlab.com/redhat/centos-stream/rpms/bash",
        "https://gitlab.com/redhat/rhel/rpms/bash",
    ],
)
@pytest.mark.parametrize(
    "fork_exists",
    [False, True],
)
@pytest.mark.asyncio
async def test_fork_repository(repository, fork_exists):
    package = "bash"
    fork_namespace = "ai-bot"
    fork_name = f"{'rhel' if '/rhel/' in repository else 'centos'}_rpms_{package}"
    clone_url = f"https://gitlab.com/{fork_namespace}/{fork_name}.git"
    fork = flexmock(
        gitlab_repo=flexmock(namespace={"full_path": fork_namespace}, path=fork_name),
        get_git_urls=lambda: {"git": clone_url},
    )
    flexmock(GitlabProject).new_instances(fork)
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=repository).and_return(
        flexmock(
            get_forks=lambda: [fork] if fork_exists else [],
            gitlab_repo=flexmock(
                forks=flexmock()
                .should_receive("create")
                .with_args(data={"name": fork_name, "path": fork_name})
                .and_return(fork.gitlab_repo)
                .mock(),
                name=package,
                namespace={
                    "full_path": repository.removeprefix("https://gitlab.com/").removesuffix(f"/{package}")
                },
                path=package,
            ),
            service=flexmock(instance_url="https://gitlab.com", user=flexmock(get_username=lambda: fork_namespace)),
        )
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
    ).and_return(flexmock(create_pr=lambda title, body, target, source: pr_mock, parent=flexmock(get_pr=lambda id: pr_mock)))
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
async def test_clone_repository():
    repository = "https://gitlab.com/ai-bot/bash.git"
    branch = "rhel-8"
    clone_path = "/git-repos/bash"

    async def create_subprocess_exec(cmd, *args, **kwargs):
        async def wait():
            return 0
        return flexmock(wait=wait)

    flexmock(asyncio).should_receive("create_subprocess_exec").with_args("git", "clone", "--single-branch", "--branch", branch, repository, clone_path).replace_with(create_subprocess_exec)
    flexmock(asyncio).should_receive("create_subprocess_exec").with_args("git", "remote", "remove", "origin", cwd=clone_path).replace_with(create_subprocess_exec)
    result = await clone_repository(repository=repository, clone_path=clone_path, branch=branch)
    assert result.startswith("Successfully")


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
