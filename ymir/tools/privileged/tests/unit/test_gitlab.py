import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import gitlab
import pytest
from beeai_framework.tools import ToolError
from flexmock import flexmock
from ogr.abstract import PRStatus
from ogr.services.gitlab import GitlabService
from ogr.services.gitlab.project import GitlabProject

from ymir.common.models import OpenMergeRequestResult
from ymir.tools.privileged.gitlab import (
    AddBlockingMergeRequestCommentTool,
    AddMergeRequestCommentTool,
    AddMergeRequestLabelsTool,
    CloneRepositoryTool,
    ForkRepositoryTool,
    GetAuthorizedCommentsFromMergeRequestTool,
    GetFailedPipelineJobsFromMergeRequestTool,
    GetPipelineJobLogTool,
    OpenMergeRequestTool,
    PushToRemoteRepositoryTool,
    RetryPipelineJobTool,
    _get_git_auth_args,
)


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
@pytest.mark.parametrize(
    "fork_namespace",
    [None, "redhat/rhel/bot-branches"],
)
@pytest.mark.asyncio
async def test_fork_repository(repository, fork_exists, fork_namespace):
    package = "bash"
    bot_username = "test-bot"
    os.environ.pop("FORK_NAMESPACE", None)
    if fork_namespace:
        os.environ["FORK_NAMESPACE"] = fork_namespace
    target_namespace = fork_namespace or bot_username
    fork_name = f"{'rhel' if '/rhel/' in repository else 'centos'}_rpms_{package}"
    clone_url = f"https://gitlab.com/{target_namespace}/{fork_name}.git"
    expected_data = {"name": fork_name, "path": fork_name}
    if fork_namespace:
        expected_data["namespace"] = fork_namespace
    fork = flexmock(
        gitlab_repo=flexmock(namespace={"full_path": target_namespace}, path=fork_name),
        get_git_urls=lambda: {"git": clone_url},
    )
    flexmock(GitlabProject).new_instances(fork)
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=repository).and_return(
        flexmock(
            get_forks=lambda: [fork] if fork_exists else [],
            gitlab_repo=flexmock(
                forks=flexmock()
                .should_receive("create")
                .with_args(data=expected_data)
                .and_return(fork.gitlab_repo)
                .mock(),
                name=package,
                namespace={
                    "full_path": repository.removeprefix("https://gitlab.com/").removesuffix(f"/{package}")
                },
                path=package,
            ),
            service=flexmock(
                instance_url="https://gitlab.com",
                user=flexmock(get_username=lambda: bot_username),
            ),
        )
    )
    assert (await ForkRepositoryTool().run(input={"repository": repository})).result == clone_url


@pytest.mark.parametrize(
    "url, fork_namespace, token, expect_auth",
    [
        # Red Hat group on gitlab.com — always needs auth when token present
        ("https://gitlab.com/redhat/centos-stream/rpms/vim", None, "tok", True),
        # gitlab.cee.redhat.com — always needs auth when token present
        ("https://gitlab.cee.redhat.com/foo/bar", None, "tok", True),
        # Fork URL under bot namespace, FORK_NAMESPACE configured — needs auth
        (
            "https://gitlab.com/redhat-ymir-agent/centos_rpms_vim.git",
            "redhat-ymir-agent",
            "tok",
            True,
        ),
        # Same fork URL but FORK_NAMESPACE not set — must NOT inject auth
        # (would leak token to unrelated gitlab.com namespace)
        ("https://gitlab.com/redhat-ymir-agent/centos_rpms_vim.git", None, "tok", False),
        # Unrelated gitlab.com namespace with FORK_NAMESPACE set — still no auth
        ("https://gitlab.com/some-other-user/repo.git", "redhat-ymir-agent", "tok", False),
        # Public github.com — never auth
        ("https://github.com/vim/vim", None, "tok", False),
        # No token configured — no auth even for Red Hat URLs
        ("https://gitlab.com/redhat/centos-stream/rpms/vim", None, None, False),
    ],
)
def test_get_git_auth_args_handles_fork_namespace(monkeypatch, url, fork_namespace, token, expect_auth):
    """Forks under FORK_NAMESPACE on gitlab.com must get the GITLAB_TOKEN injected for git push."""
    monkeypatch.delenv("FORK_NAMESPACE", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    if fork_namespace:
        monkeypatch.setenv("FORK_NAMESPACE", fork_namespace)
    if token:
        monkeypatch.setenv("GITLAB_TOKEN", token)

    args = _get_git_auth_args(url)
    if expect_auth:
        assert len(args) == 2
        assert args[0] == "-c"
        assert args[1].startswith("http.extraheader=Authorization: Basic ")
    else:
        assert args == []


@pytest.mark.asyncio
async def test_open_merge_request():
    fork_url = "https://gitlab.com/ai-bot/bash.git"
    title = "Fix RHEL-12345"
    description = "Resolves RHEL-12345"
    target = "c10s"
    source = "automated-package-update-RHEL-12345"
    mr_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/1"
    parent_project_id = 42
    raw_mr_mock = flexmock(web_url=mr_url, iid=1, author={"username": "bot"})
    mr_manager_mock = flexmock()
    expected_params = {
        "source_branch": source,
        "target_branch": target,
        "title": title,
        "description": description,
        "target_project_id": parent_project_id,
    }
    mr_manager_mock.should_receive("create").with_args(expected_params).and_return(raw_mr_mock).once()

    parent_gitlab_repo = flexmock(attributes={"id": parent_project_id})
    parent_project = flexmock(gitlab_repo=parent_gitlab_repo)
    fork_project = flexmock(
        is_fork=True,
        parent=parent_project,
        gitlab_repo=flexmock(mergerequests=mr_manager_mock),
    )
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=fork_url).and_return(
        fork_project
    )
    out = await OpenMergeRequestTool().run(
        input={
            "fork_url": fork_url,
            "title": title,
            "description": description,
            "target": target,
            "source": source,
        }
    )
    assert out.result == OpenMergeRequestResult(url=mr_url, is_new_mr=True)


@pytest.mark.asyncio
async def test_open_merge_request_with_labels():
    fork_url = "https://gitlab.com/ai-bot/bash.git"
    title = "Fix RHEL-12345"
    description = "Resolves RHEL-12345"
    target = "c10s"
    source = "automated-package-update-RHEL-12345"
    mr_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/1"
    parent_project_id = 42
    raw_mr_mock = flexmock(web_url=mr_url, iid=1, author={"username": "bot"})
    mr_manager_mock = flexmock()
    expected_params = {
        "source_branch": source,
        "target_branch": target,
        "title": title,
        "description": description,
        "labels": "ymir_backport,target::zstream",
        "target_project_id": parent_project_id,
    }
    mr_manager_mock.should_receive("create").with_args(expected_params).and_return(raw_mr_mock).once()

    parent_gitlab_repo = flexmock(attributes={"id": parent_project_id})
    parent_project = flexmock(gitlab_repo=parent_gitlab_repo)
    fork_project = flexmock(
        is_fork=True,
        parent=parent_project,
        gitlab_repo=flexmock(mergerequests=mr_manager_mock),
    )
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=fork_url).and_return(
        fork_project
    )
    out = await OpenMergeRequestTool().run(
        input={
            "fork_url": fork_url,
            "title": title,
            "description": description,
            "target": target,
            "source": source,
            "labels": ["ymir_backport", "target::zstream"],
        }
    )
    assert out.result == OpenMergeRequestResult(url=mr_url, is_new_mr=True)


@pytest.mark.asyncio
async def test_open_merge_request_with_existing_mr():
    fork_url = "https://gitlab.com/ai-bot/bash.git"
    title = "Fix RHEL-12345"
    description = "Resolves RHEL-12345"
    target = "c10s"
    source = "automated-package-update-RHEL-12345"
    mr_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/1"
    parent_project_id = 42
    pr_mock = flexmock(
        url=mr_url,
        source_branch=source,
        status=PRStatus.open,
        target_branch=target,
        id=1,
    )

    # mergerequests.create raises 409 indicating the MR already exists
    mr_manager_mock = flexmock()
    mr_manager_mock.should_receive("create").and_raise(gitlab.GitlabError(response_code=409))

    parent_gitlab_repo = flexmock(attributes={"id": parent_project_id})
    parent_project = flexmock(
        gitlab_repo=parent_gitlab_repo,
        get_pr_list=lambda: [pr_mock],
    )
    fork_project = flexmock(
        is_fork=True,
        parent=parent_project,
        gitlab_repo=flexmock(mergerequests=mr_manager_mock),
    )
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=fork_url).and_return(
        fork_project
    )
    out = await OpenMergeRequestTool().run(
        input={
            "fork_url": fork_url,
            "title": title,
            "description": description,
            "target": target,
            "source": source,
        }
    )
    assert out.result == OpenMergeRequestResult(url=mr_url, is_new_mr=False)


@pytest.mark.asyncio
async def test_clone_repository(mock_git_repo_basepath):
    repository = "https://gitlab.com/centos-stream/rpms/bash"
    branch = "rhel-8.10.0"
    clone_path = mock_git_repo_basepath / "bash"

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

    result = (
        await CloneRepositoryTool().run(
            input={"repository": repository, "branch": branch, "clone_path": clone_path}
        )
    ).result
    assert result.startswith("Successfully")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_path",
    [
        Path("/tmp/bash"),
        Path("/var/lib/bash"),
    ],
    ids=["outside-base", "unrelated-absolute"],
)
async def test_clone_repository_rejects_path_outside_basepath(mock_git_repo_basepath, bad_path):
    with pytest.raises(ToolError, match="must be under"):
        await CloneRepositoryTool().run(
            input={"repository": "https://gitlab.com/redhat/rhel/rpms/bash", "clone_path": bad_path}
        )


@pytest.mark.asyncio
async def test_clone_repository_rejects_path_traversal(mock_git_repo_basepath):
    traversal_path = mock_git_repo_basepath / ".." / "tmp" / "bash"
    with pytest.raises(ToolError, match="must be under"):
        await CloneRepositoryTool().run(
            input={"repository": "https://gitlab.com/redhat/rhel/rpms/bash", "clone_path": traversal_path}
        )


@pytest.mark.asyncio
async def test_clone_repository_rejects_basepath_root(mock_git_repo_basepath):
    with pytest.raises(ToolError, match="must be under"):
        await CloneRepositoryTool().run(
            input={
                "repository": "https://gitlab.com/redhat/rhel/rpms/bash",
                "clone_path": mock_git_repo_basepath,
            }
        )


@pytest.mark.asyncio
async def test_clone_repository_accepts_path_inside_basepath(mock_git_repo_basepath):
    valid_path = mock_git_repo_basepath / "RHEL-12345" / "bash"

    async def create_subprocess_exec(cmd, *args, **kwargs):
        async def wait():
            return 0

        return flexmock(wait=wait)

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(create_subprocess_exec)

    result = (
        await CloneRepositoryTool().run(
            input={"repository": "https://gitlab.com/redhat/rhel/rpms/bash", "clone_path": valid_path}
        )
    ).result
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
    result = (
        await PushToRemoteRepositoryTool().run(
            input={"repository": repository, "clone_path": clone_path, "branch": branch}
        )
    ).result
    assert result.startswith("Successfully")


@pytest.mark.parametrize(
    "merge_request_url,expected_project_path",
    [
        (
            "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123",
            "redhat/rhel/rpms/bash",
        ),
        (
            "https://gitlab.com/packit-service/hello-world/-/merge_requests/123",
            "packit-service/hello-world",
        ),
    ],
)
@pytest.mark.asyncio
async def test_add_merge_request_labels(merge_request_url, expected_project_path):
    labels = ["test-label-1", "test-label-2"]

    # Mock the merge request object
    mr_mock = flexmock()
    mr_mock.should_receive("add_label").with_args("test-label-1").once()
    mr_mock.should_receive("add_label").with_args("test-label-2").once()

    # Mock the project object
    project_mock = flexmock()
    project_mock.should_receive("get_pr").and_return(mr_mock)

    # Mock GitlabService.get_project_from_url
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=f"https://gitlab.com/{expected_project_path}"
    ).and_return(project_mock)

    result = (
        await AddMergeRequestLabelsTool().run(
            input={"merge_request_url": merge_request_url, "labels": labels}
        )
    ).result

    assert result == f"Successfully added labels {labels} to merge request {merge_request_url}"


@pytest.mark.asyncio
async def test_add_merge_request_labels_invalid_url():
    merge_request_url = "https://github.com/user/repo/pull/123"
    labels = ["test-label"]

    with pytest.raises(Exception) as exc_info:
        await AddMergeRequestLabelsTool().run(
            input={"merge_request_url": merge_request_url, "labels": labels}
        )

    assert "Could not parse merge request URL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_add_merge_request_comment():
    merge_request_url = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123"
    comment = "Test comment"

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=merge_request_url.rsplit("/-/merge_requests/", 1)[0],
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .and_return(
            flexmock(
                id=123,
                _raw_pr=flexmock(
                    notes=flexmock()
                    .should_receive("create")
                    .with_args(
                        {"body": comment},
                    )
                    .and_return(
                        flexmock(id=1),
                    )
                    .mock(),
                ),
            ),
        )
        .mock()
    )

    result = (
        await AddMergeRequestCommentTool().run(
            input={"merge_request_url": merge_request_url, "comment": comment}
        )
    ).result

    assert result == f"Successfully added comment to merge request {merge_request_url}"


@pytest.mark.asyncio
async def test_add_blocking_merge_request_comment():
    merge_request_url = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123"
    comment = "**Blocking Merge Request**\n\nTest comment"

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        # Extract project URL from merge request URL
        url=merge_request_url.rsplit("/-/merge_requests/", 1)[0],
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .and_return(
            flexmock(
                id=123,
                _raw_pr=flexmock(
                    discussions=flexmock()
                    .should_receive("list")
                    .with_args(get_all=True)
                    .and_return([])
                    .mock()
                    .should_receive("create")
                    .with_args({"body": comment})
                    .and_return(flexmock(id=1))
                    .mock(),
                ),
            ),
        )
        .mock()
    )

    result = (
        await AddBlockingMergeRequestCommentTool().run(
            input={"merge_request_url": merge_request_url, "comment": comment}
        )
    ).result

    assert result == f"Successfully added blocking comment to merge request {merge_request_url}"


@pytest.mark.parametrize("resolved_status", [False, True])
@pytest.mark.asyncio
async def test_add_blocking_merge_request_comment_already_exists(resolved_status):
    merge_request_url = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123"
    comment = "**Blocking Merge Request**\n\nTest comment"

    existing_discussion = flexmock(
        id="disc1",
        attributes={
            "notes": [{"body": "**Blocking Merge Request**\n\nTest comment"}],
            "resolved": resolved_status,
        },
    )

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=merge_request_url.rsplit("/-/merge_requests/", 1)[0],
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .and_return(
            flexmock(
                id=123,
                _raw_pr=flexmock(
                    discussions=flexmock()
                    .should_receive("list")
                    .with_args(get_all=True)
                    .and_return([existing_discussion])
                    .mock()
                ),
            ),
        )
        .mock()
    )

    result = (
        await AddBlockingMergeRequestCommentTool().run(
            input={"merge_request_url": merge_request_url, "comment": comment}
        )
    ).result

    assert "already exists" in result
    assert merge_request_url in result


@pytest.mark.asyncio
async def test_add_blocking_merge_request_comment_invalid_url():
    merge_request_url = "https://github.com/user/repo/pull/123"
    comment = "Test comment"

    with pytest.raises(Exception) as exc_info:
        await AddBlockingMergeRequestCommentTool().run(
            input={"merge_request_url": merge_request_url, "comment": comment}
        )

    assert "Could not parse merge request URL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_retry_pipeline_job():
    project_url = "https://gitlab.com/redhat/rhel/rpms/bash"
    job_id = 12345678

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=project_url).and_return(
        flexmock(
            gitlab_repo=flexmock(
                jobs=flexmock()
                .should_receive("get")
                .with_args(job_id)
                .and_return(flexmock(id=job_id, status="pending").should_receive("retry").once().mock())
                .mock()
            )
        )
    )

    result = (await RetryPipelineJobTool().run(input={"project_url": project_url, "job_id": job_id})).result

    assert result == f"Successfully retried job {job_id}. Status: pending"


@pytest.mark.asyncio
async def test_retry_pipeline_job_invalid_project():
    project_url = "https://gitlab.com/nonexistent/project"
    job_id = 12345678

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=project_url).and_raise(
        Exception("Project not found")
    )

    with pytest.raises(Exception) as exc_info:
        await RetryPipelineJobTool().run(input={"project_url": project_url, "job_id": job_id})

    assert "Failed to retry job" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_failed_pipeline_jobs_from_merge_request():
    merge_request_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/123"
    pipeline_id = 789

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url="https://gitlab.com/redhat/centos-stream/rpms/bash"
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .with_args(123)
        .and_return(
            flexmock(
                _raw_pr=flexmock(head_pipeline={"id": pipeline_id}),
                target_project=flexmock(
                    namespace="redhat/centos-stream/rpms",
                    repo="bash",
                    gitlab_repo=flexmock(
                        pipelines=flexmock()
                        .should_receive("get")
                        .with_args(pipeline_id)
                        .and_return(
                            flexmock(
                                jobs=flexmock()
                                .should_receive("list")
                                .with_args(get_all=True)
                                .and_return(
                                    [
                                        flexmock(
                                            id=11111,
                                            name="check-tickets",
                                            status="failed",
                                            stage="build",
                                            artifacts_file={"filename": "debug.log"},
                                        ),
                                        flexmock(
                                            id=22222,
                                            name="build_rpm",
                                            status="failed",
                                            stage="test",
                                            artifacts_file=None,
                                        ),
                                        flexmock(
                                            id=33333,
                                            name="trigger_tests",
                                            status="success",
                                            stage="test",
                                            artifacts_file=None,
                                        ),
                                    ]
                                )
                                .mock()
                            )
                        )
                        .mock()
                    ),
                ),
            )
        )
        .mock()
    )

    result = (
        await GetFailedPipelineJobsFromMergeRequestTool().run(input={"merge_request_url": merge_request_url})
    ).result

    assert len(result) == 2
    assert result[0].id == "11111"
    assert result[0].name == "check-tickets"
    assert result[0].status == "failed"
    assert result[0].stage == "build"
    assert "/-/jobs/11111" in result[0].url
    assert (
        result[0].artifacts_url
        == "https://gitlab.com/redhat/centos-stream/rpms/bash/-/jobs/11111/artifacts/browse"
    )

    assert result[1].id == "22222"
    assert result[1].name == "build_rpm"
    assert result[1].status == "failed"
    assert result[1].stage == "test"
    assert "/-/jobs/22222" in result[1].url
    assert result[1].artifacts_url == ""


@pytest.mark.asyncio
async def test_get_failed_pipeline_jobs_from_merge_request_no_pipelines():
    merge_request_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/123"

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url="https://gitlab.com/redhat/centos-stream/rpms/bash"
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .with_args(123)
        .and_return(flexmock(_raw_pr=flexmock(head_pipeline=None)))
        .mock()
    )

    result = (
        await GetFailedPipelineJobsFromMergeRequestTool().run(input={"merge_request_url": merge_request_url})
    ).result

    assert len(result) == 0


@pytest.mark.asyncio
async def test_get_failed_pipeline_jobs_from_merge_request_invalid_url():
    merge_request_url = "https://github.com/user/repo/pull/123"

    with pytest.raises(Exception) as exc_info:
        await GetFailedPipelineJobsFromMergeRequestTool().run(input={"merge_request_url": merge_request_url})

    assert "Could not parse merge request URL" in str(exc_info.value)


@pytest.mark.parametrize(
    "discussions,members,expected_count",
    [
        pytest.param(
            [
                flexmock(
                    id="d1",
                    attributes={
                        "notes": [
                            {
                                "author": {"id": 1, "username": "dev"},
                                "body": "Dev",
                                "created_at": "2024-01-15T10:00:00Z",
                                "system": False,
                            }
                        ]
                    },
                ),
                flexmock(
                    id="d2",
                    attributes={
                        "notes": [
                            {
                                "author": {"id": 2, "username": "reporter"},
                                "body": "Rep",
                                "created_at": "2024-01-15T11:00:00Z",
                                "system": False,
                            }
                        ]
                    },
                ),
                flexmock(
                    id="d3",
                    attributes={
                        "notes": [
                            {
                                "author": {"id": 3, "username": "guest"},
                                "body": "Guest",
                                "created_at": "2024-01-15T12:00:00Z",
                                "system": False,
                            }
                        ]
                    },
                ),
            ],
            [flexmock(id=1, access_level=30), flexmock(id=2, access_level=20)],
            1,
            id="filters_unauthorized",
        ),
        pytest.param(
            [],
            [],
            0,
            id="no_comments",
        ),
        pytest.param(
            [
                flexmock(
                    id="d1",
                    attributes={
                        "notes": [
                            {
                                "author": {"id": 1, "username": "dev1"},
                                "body": "General",
                                "created_at": "2024-01-15T10:00:00Z",
                                "system": False,
                            }
                        ]
                    },
                ),
                flexmock(
                    id="d2",
                    attributes={
                        "notes": [
                            {
                                "author": {"id": 2, "username": "dev2"},
                                "body": "Line",
                                "created_at": "2024-01-15T11:00:00Z",
                                "system": False,
                                "position": {
                                    "new_path": "f.py",
                                    "old_path": "f.py",
                                    "new_line": 42,
                                    "old_line": None,
                                },
                            }
                        ]
                    },
                ),
            ],
            [flexmock(id=1, access_level=30), flexmock(id=2, access_level=30)],
            2,
            id="with_line_context",
        ),
        pytest.param(
            [
                flexmock(
                    id="d1",
                    attributes={
                        "notes": [
                            {
                                "author": {"id": 1, "username": "dev1"},
                                "body": "Q",
                                "created_at": "2024-01-15T10:00:00Z",
                                "system": False,
                            },
                            {
                                "author": {"id": 2, "username": "dev2"},
                                "body": "A",
                                "created_at": "2024-01-15T10:30:00Z",
                                "system": False,
                            },
                            {
                                "author": {"id": 3, "username": "guest"},
                                "body": "?",
                                "created_at": "2024-01-15T10:45:00Z",
                                "system": False,
                            },
                        ]
                    },
                ),
            ],
            [
                flexmock(id=1, access_level=30),
                flexmock(id=2, access_level=30),
                flexmock(id=3, access_level=10),
            ],
            1,
            id="with_replies",
        ),
    ],
)
@pytest.mark.asyncio
async def test_get_authorized_comments_from_merge_request(discussions, members, expected_count):
    merge_request_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/123"

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url="https://gitlab.com/redhat/centos-stream/rpms/bash"
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .with_args(123)
        .and_return(
            flexmock(
                _raw_pr=flexmock(
                    discussions=flexmock()
                    .should_receive("list")
                    .with_args(get_all=True)
                    .and_return(discussions)
                    .mock()
                ),
                target_project=flexmock(
                    namespace="redhat/centos-stream/rpms",
                    repo="bash",
                    gitlab_repo=flexmock(
                        members_all=flexmock()
                        .should_receive("list")
                        .with_args(get_all=True)
                        .and_return(members)
                        .mock()
                    ),
                ),
            )
        )
        .mock()
    )

    result = (
        await GetAuthorizedCommentsFromMergeRequestTool().run(input={"merge_request_url": merge_request_url})
    ).result

    assert len(result) == expected_count


@pytest.mark.asyncio
async def test_get_authorized_comments_invalid_url():
    """Test that invalid URLs raise appropriate errors."""
    with pytest.raises(Exception) as exc_info:
        await GetAuthorizedCommentsFromMergeRequestTool().run(
            input={"merge_request_url": "https://github.com/user/repo/pull/123"}
        )
    assert "Could not parse merge request URL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_pipeline_job_log():
    """Verify GetPipelineJobLogTool fetches job trace and returns log text."""
    project_path = "redhat/rhel/rpms/curl"
    job_id = "12345"
    log_content = "Running tests...\nFAILED: test_curl_connect\nExit code: 1"

    @asynccontextmanager
    async def mock_get(url, **kwargs):
        assert f"/projects/redhat%2Frhel%2Frpms%2Fcurl/jobs/{job_id}/trace" in url

        async def read():
            return log_content.encode("utf-8")

        yield flexmock(status=200, read=read, raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(mock_get)
    flexmock(os).should_call("getenv")
    flexmock(os).should_receive("getenv").with_args("GITLAB_TOKEN").and_return("test-token").once()

    tool = GetPipelineJobLogTool()
    result = await tool.run(
        input={"project_path": project_path, "job_id": job_id},
    )
    assert "FAILED: test_curl_connect" in result.result


@pytest.mark.asyncio
async def test_get_pipeline_job_log_truncates_large_output():
    """Verify logs exceeding MAX_LOG_LINES are truncated to last N lines."""
    project_path = "redhat/rhel/rpms/curl"
    job_id = "12345"
    long_log = "\n".join(f"line {i}" for i in range(1000))

    @asynccontextmanager
    async def mock_get(url, **kwargs):
        async def read():
            return long_log.encode("utf-8")

        yield flexmock(status=200, read=read, raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(mock_get)
    flexmock(os).should_call("getenv")
    flexmock(os).should_receive("getenv").with_args("GITLAB_TOKEN").and_return("test-token").once()

    tool = GetPipelineJobLogTool()
    result = await tool.run(
        input={"project_path": project_path, "job_id": job_id},
    )
    assert "[... truncated, showing last 500 of 1000 lines ...]" in result.result
    assert "line 999" in result.result
    assert "line 0" not in result.result
