import asyncio

import gitlab
import pytest

from pathlib import Path

from ogr.abstract import PRStatus
from ogr.exceptions import GitlabAPIException
from ogr.services.gitlab.project import GitlabProject
from flexmock import flexmock
from ogr.services.gitlab import GitlabService

from gitlab_tools import (
    clone_repository,
    fork_repository,
    open_merge_request,
    push_to_remote_repository,
    add_merge_request_labels,
    add_blocking_merge_request_comment,
)
from test_utils import mock_git_repo_basepath



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
async def test_clone_repository(mock_git_repo_basepath):
    repository = "https://gitlab.com/centos-stream/rpms/bash"
    branch = "rhel-8.10.0"
    clone_path = Path("/git-repos/bash")

    async def create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "git"
        assert kwargs.get("cwd") == clone_path
        if args[0] == "init":
            assert len(args) == 1
        elif args[0] == "fetch":
            assert args[1].endswith(repository.removeprefix("https://"))
            assert args[2] == f"{branch}:refs/heads/{branch}"
        elif args[0] == "checkout":
            assert args[1] == branch
        else:
            pytest.fail(f"Unexpected git command: {args}")
        async def wait():
            return 0
        return flexmock(wait=wait)

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)

    result = await clone_repository(repository=repository, branch=branch, clone_path=clone_path)
    assert result.startswith("Successfully")


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


@pytest.mark.parametrize(
    "merge_request_url,expected_project_path",
    [
        ("https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123", "redhat/rhel/rpms/bash"),
        ("https://gitlab.com/packit-service/hello-world/-/merge_requests/123", "packit-service/hello-world"),
    ],
)
@pytest.mark.asyncio
async def test_add_merge_request_labels(merge_request_url, expected_project_path):
    labels = ["jotnar_fusa", "test-label"]

    # Mock the merge request object
    mr_mock = flexmock()
    mr_mock.should_receive("add_label").with_args("jotnar_fusa").once()
    mr_mock.should_receive("add_label").with_args("test-label").once()

    # Mock the project object
    project_mock = flexmock()
    project_mock.should_receive("get_pr").and_return(mr_mock)

    # Mock GitlabService.get_project_from_url
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=f"https://gitlab.com/{expected_project_path}"
    ).and_return(project_mock)

    result = await add_merge_request_labels(
        merge_request_url=merge_request_url,
        labels=labels
    )

    assert result == f"Successfully added labels {labels} to merge request {merge_request_url}"


@pytest.mark.asyncio
async def test_add_merge_request_labels_invalid_url():
    merge_request_url = "https://github.com/user/repo/pull/123"
    labels = ["test-label"]

    with pytest.raises(Exception) as exc_info:
        await add_merge_request_labels(
            merge_request_url=merge_request_url,
            labels=labels
        )

    assert "Could not parse merge request URL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_add_blocking_merge_request_comment():
    merge_request_url = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123"
    comment = "**Blocking Merge Request**\n\nTest comment"

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        # Extract project URL from merge request URL
        url=merge_request_url.rsplit("/-/merge_requests/", 1)[0],
    ).and_return(
        flexmock().should_receive("get_pr").and_return(
            flexmock(
                id=123,
                _raw_pr=flexmock(
                    discussions=flexmock().should_receive("create").with_args({"body": comment}).and_return(
                        flexmock(id=1),
                    ).mock(),
                ),
            ),
        ).mock()
    )

    result = await add_blocking_merge_request_comment(
        merge_request_url=merge_request_url,
        comment=comment
    )

    assert result == f"Successfully added blocking comment to merge request {merge_request_url}"


@pytest.mark.asyncio
async def test_add_blocking_merge_request_comment_invalid_url():
    merge_request_url = "https://github.com/user/repo/pull/123"
    comment = "Test comment"

    with pytest.raises(Exception) as exc_info:
        await add_blocking_merge_request_comment(
            merge_request_url=merge_request_url,
            comment=comment
        )

    assert "Could not parse merge request URL" in str(exc_info.value)
