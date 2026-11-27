import asyncio
import json
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import gitlab
import pytest
import requests
from beeai_framework.tools import ToolError
from beeai_framework.tools.errors import ToolInputValidationError
from flexmock import flexmock
from ogr.abstract import PRStatus
from ogr.exceptions import GitlabAPIException
from ogr.services.gitlab import GitlabService
from ogr.services.gitlab.project import GitlabProject

from ymir.common import constants, http_utils
from ymir.common.constants import GITLAB_MR_PAGE_SIZE
from ymir.common.models import OpenMergeRequestResult
from ymir.tools.privileged import gitlab as gitlab_module
from ymir.tools.privileged import gitlab as gitlab_tool_module
from ymir.tools.privileged import reviewer_resolver as _resolver
from ymir.tools.privileged.gitlab import (
    MAX_PATCH_CONTENT_LENGTH,
    AddBlockingMergeRequestCommentTool,
    AddMergeRequestCommentTool,
    AddMergeRequestLabelsTool,
    CloneRepositoryTool,
    FetchBranchTool,
    FetchCommitTool,
    ForkRepositoryTool,
    GetAuthorizedCommentsFromMergeRequestTool,
    GetFailedPipelineJobsFromMergeRequestTool,
    GetMergeRequestCommitMessagesTool,
    GetMergeRequestDetailsTool,
    GetMergeRequestDiscussionsTool,
    GetPatchFromUrlFullTool,
    GetPatchFromUrlTool,
    GetPipelineJobTraceTool,
    GetRemoteBranchHeadTool,
    ListProjectMergeRequestsTool,
    OpenMergeRequestTool,
    PushToRemoteRepositoryTool,
    RemoveMergeRequestLabelsTool,
    ResolveMergeRequestDiscussionTool,
    ResolveQeReviewersTool,
    ResolveReviewersTool,
    RetryPipelineJobTool,
    SetMergeRequestReviewersTool,
    UpdateMergeRequestCommentTool,
    _get_api_diff_url,
    _get_git_auth_args,
    _parse_merge_request_url,
    _remove_existing_clone_path,
    _wait_for_fork_ready,
)
from ymir.tools.privileged.utils import sanitize_url


def _fork_project_mock(
    *,
    target_namespace: str,
    fork_name: str,
    clone_url: str,
    import_status: str = "finished",
):
    gitlab_repo = flexmock(
        namespace={"full_path": target_namespace},
        path=fork_name,
        attributes={"import_status": import_status},
    )
    service = flexmock(
        gitlab_instance=flexmock(projects=flexmock(get=lambda _path: gitlab_repo)),
    )
    return flexmock(
        namespace=target_namespace,
        repo=fork_name,
        gitlab_repo=gitlab_repo,
        service=service,
        get_git_urls=lambda: {"git": clone_url},
    )


@pytest.mark.parametrize(
    ("merge_request_url", "expected"),
    [
        (
            "https://gitlab.com/redhat/rhel/tests/shared/-/merge_requests/42",
            ("redhat/rhel/tests/shared", 42),
        ),
        (
            "https://gitlab.com/redhat/group/subgroup/nested/project/-/merge_requests/123/diffs",
            ("redhat/group/subgroup/nested/project", 123),
        ),
    ],
)
def test_parse_merge_request_url_accepts_nested_project_paths(merge_request_url, expected):
    assert _parse_merge_request_url(merge_request_url) == expected


@pytest.mark.parametrize(
    "merge_request_url",
    [
        "https://github.com/redhat/group/project/-/merge_requests/1",
        "https://gitlab.com/redhat/-/merge_requests/1",
        "https://gitlab.com/redhat/group/project/-/merge_requests/not-an-iid",
    ],
)
def test_parse_merge_request_url_rejects_invalid_urls(merge_request_url):
    with pytest.raises(ToolError, match="Could not parse merge request URL"):
        _parse_merge_request_url(merge_request_url)


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
    fork = _fork_project_mock(
        target_namespace=target_namespace,
        fork_name=fork_name,
        clone_url=clone_url,
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


@pytest.mark.asyncio
async def test_fork_repository_rejects_existing_failed_fork(monkeypatch):
    repository = "https://gitlab.com/redhat/rhel/rpms/dpdk"
    fork_namespace = "redhat/rhel/bot-branches"
    monkeypatch.setenv("FORK_NAMESPACE", fork_namespace)
    fork_name = "rhel_rpms_dpdk"
    fork = _fork_project_mock(
        target_namespace=fork_namespace,
        fork_name=fork_name,
        clone_url=f"https://gitlab.com/{fork_namespace}/{fork_name}.git",
        import_status="failed",
    )
    fork.gitlab_repo.attributes["import_error"] = "Import timed out"
    flexmock(GitlabProject).new_instances(fork)
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=repository).and_return(
        flexmock(
            get_forks=lambda: [fork],
            gitlab_repo=flexmock(name="dpdk", namespace={"full_path": "redhat/rhel/rpms"}),
            service=flexmock(
                instance_url="https://gitlab.com", user=flexmock(get_username=lambda: "test-bot")
            ),
        )
    )

    with pytest.raises(ToolError) as exc_info:
        await ForkRepositoryTool().run(input={"repository": repository})
    assert "failed to import" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_fork_repository_fetches_status_when_fork_list_omits_it(monkeypatch):
    repository = "https://gitlab.com/redhat/rhel/rpms/dpdk"
    fork_namespace = "redhat/rhel/bot-branches"
    monkeypatch.setenv("FORK_NAMESPACE", fork_namespace)
    fork_name = "rhel_rpms_dpdk"
    listed_repo = flexmock(
        namespace={"full_path": fork_namespace},
        attributes={},
    )
    fetched_repo = _fork_project_mock(
        target_namespace=fork_namespace,
        fork_name=fork_name,
        clone_url=f"https://gitlab.com/{fork_namespace}/{fork_name}.git",
        import_status="failed",
    ).gitlab_repo
    fetched_repo.attributes["import_error"] = "Import timed out"
    fork = flexmock(
        namespace=fork_namespace,
        repo=fork_name,
        gitlab_repo=listed_repo,
        service=flexmock(gitlab_instance=flexmock(projects=flexmock(get=lambda _path: fetched_repo))),
        get_git_urls=lambda: {"git": f"https://gitlab.com/{fork_namespace}/{fork_name}.git"},
    )
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=repository).and_return(
        flexmock(
            get_forks=lambda: [fork],
            gitlab_repo=flexmock(name="dpdk", namespace={"full_path": "redhat/rhel/rpms"}),
            service=flexmock(
                instance_url="https://gitlab.com", user=flexmock(get_username=lambda: "test-bot")
            ),
        )
    )

    with pytest.raises(ToolError) as exc_info:
        await ForkRepositoryTool().run(input={"repository": repository})
    assert "failed to import" in str(exc_info.value.__cause__)


def _mock_original_project(*, repository, package, bot_username, fork, expected_data, fork_exists=False):
    """Mock get_project_from_url for a redhat gitlab.com project (no fork yet)."""
    original_git_url = f"{repository}.git"
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=repository).and_return(
        flexmock(
            get_forks=lambda: [fork] if fork_exists else [],
            get_git_urls=lambda: {"git": original_git_url},
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
    return original_git_url


@pytest.mark.asyncio
async def test_fork_repository_copr_dry_run_creates_fork(monkeypatch):
    """Both backends build after commit+push, so DRY_RUN creates the fork for Copr too."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("BUILD_BACKEND", raising=False)  # default copr
    monkeypatch.setenv("FORK_NAMESPACE", "redhat/rhel/bot-branches")
    repository = "https://gitlab.com/redhat/centos-stream/rpms/bash"
    package = "bash"
    clone_url = "https://gitlab.com/redhat/rhel/bot-branches/centos_rpms_bash.git"
    fork = _fork_project_mock(
        target_namespace="redhat/rhel/bot-branches",
        fork_name="centos_rpms_bash",
        clone_url=clone_url,
    )
    flexmock(GitlabProject).new_instances(fork)
    expected_data = {
        "name": "centos_rpms_bash",
        "path": "centos_rpms_bash",
        "namespace": "redhat/rhel/bot-branches",
    }
    _mock_original_project(
        repository=repository,
        package=package,
        bot_username="test-bot",
        fork=fork,
        expected_data=expected_data,
    )
    result = (await ForkRepositoryTool().run(input={"repository": repository})).result
    assert result == clone_url


@pytest.mark.asyncio
async def test_fork_repository_konflux_dry_run_creates_fork(monkeypatch):
    """Konflux builds from a pushed fork ref, so the fork is created even under DRY_RUN."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("BUILD_BACKEND", "konflux")
    monkeypatch.setenv("FORK_NAMESPACE", "redhat/rhel/bot-branches")
    repository = "https://gitlab.com/redhat/centos-stream/rpms/bash"
    package = "bash"
    clone_url = "https://gitlab.com/redhat/rhel/bot-branches/centos_rpms_bash.git"
    fork = _fork_project_mock(
        target_namespace="redhat/rhel/bot-branches",
        fork_name="centos_rpms_bash",
        clone_url=clone_url,
    )
    flexmock(GitlabProject).new_instances(fork)
    expected_data = {
        "name": "centos_rpms_bash",
        "path": "centos_rpms_bash",
        "namespace": "redhat/rhel/bot-branches",
    }
    _mock_original_project(
        repository=repository,
        package=package,
        bot_username="test-bot",
        fork=fork,
        expected_data=expected_data,
    )
    result = (await ForkRepositoryTool().run(input={"repository": repository})).result
    assert result == clone_url


def test_wait_for_fork_ready_returns_when_import_finished():
    fork = _fork_project_mock(
        target_namespace="redhat/rhel/bot-branches",
        fork_name="rhel_tests_expat",
        clone_url="https://gitlab.com/redhat/rhel/bot-branches/rhel_tests_expat.git",
        import_status="finished",
    )
    _wait_for_fork_ready(fork)


def test_wait_for_fork_ready_polls_until_finished(monkeypatch):
    class Repo:
        def __init__(self):
            self.namespace = {"full_path": "redhat/rhel/bot-branches"}
            self.path = "rhel_tests_expat"
            self.attributes = {"import_status": "started"}

    repo = Repo()
    get_count = 0

    def get_project(_path):
        nonlocal get_count
        get_count += 1
        if get_count >= 2:
            repo.attributes["import_status"] = "finished"
        return repo

    fork = flexmock(
        namespace="redhat/rhel/bot-branches",
        repo="rhel_tests_expat",
        gitlab_repo=repo,
        service=flexmock(
            gitlab_instance=flexmock(projects=flexmock(get=get_project)),
        ),
        get_git_urls=lambda: {"git": "https://gitlab.com/redhat/rhel/bot-branches/rhel_tests_expat.git"},
    )
    monkeypatch.setattr(
        "ymir.tools.privileged.gitlab._FORK_READY_POLL_INTERVAL_SEC",
        0,
    )
    _wait_for_fork_ready(fork)
    assert get_count == 2


def test_wait_for_fork_ready_raises_on_failed_import():
    fork = _fork_project_mock(
        target_namespace="redhat/rhel/bot-branches",
        fork_name="rhel_tests_expat",
        clone_url="https://gitlab.com/redhat/rhel/bot-branches/rhel_tests_expat.git",
        import_status="failed",
    )
    fork.gitlab_repo.attributes["import_error"] = "fork import blew up"
    with pytest.raises(ToolError, match="failed to import"):
        _wait_for_fork_ready(fork)


def test_wait_for_fork_ready_wraps_gitlab_error_from_project_get():
    fork = flexmock(
        namespace="redhat/rhel/bot-branches",
        repo="rhel_tests_foo",
        service=flexmock(
            gitlab_instance=flexmock(
                projects=flexmock(
                    get=lambda _path: (_ for _ in ()).throw(gitlab.GitlabGetError("403", 403, b""))
                )
            ),
        ),
    )
    with pytest.raises(ToolError, match="Failed to query fork project"):
        _wait_for_fork_ready(fork)


def test_wait_for_fork_ready_retries_transient_project_get(monkeypatch):
    repo = _fork_project_mock(
        target_namespace="redhat/rhel/bot-branches",
        fork_name="rhel_tests_expat",
        clone_url="https://gitlab.com/redhat/rhel/bot-branches/rhel_tests_expat.git",
    ).gitlab_repo
    get_count = 0

    def get_project(_path):
        nonlocal get_count
        get_count += 1
        if get_count == 1:
            raise gitlab.GitlabGetError("503", 503, b"")
        return repo

    fork = flexmock(
        namespace="redhat/rhel/bot-branches",
        repo="rhel_tests_expat",
        gitlab_repo=repo,
        service=flexmock(gitlab_instance=flexmock(projects=flexmock(get=get_project))),
    )
    monkeypatch.setattr(
        "ymir.tools.privileged.gitlab._FORK_READY_POLL_INTERVAL_SEC",
        0,
    )

    _wait_for_fork_ready(fork)
    assert get_count == 2


def test_wait_for_fork_ready_retries_connection_error(monkeypatch):
    repo = _fork_project_mock(
        target_namespace="redhat/rhel/bot-branches",
        fork_name="rhel_tests_expat",
        clone_url="https://gitlab.com/redhat/rhel/bot-branches/rhel_tests_expat.git",
    ).gitlab_repo
    get_count = 0

    def get_project(_path):
        nonlocal get_count
        get_count += 1
        if get_count == 1:
            raise requests.ConnectionError("connection reset")
        return repo

    fork = flexmock(
        namespace="redhat/rhel/bot-branches",
        repo="rhel_tests_expat",
        gitlab_repo=repo,
        service=flexmock(gitlab_instance=flexmock(projects=flexmock(get=get_project))),
    )
    monkeypatch.setattr(
        "ymir.tools.privileged.gitlab._FORK_READY_POLL_INTERVAL_SEC",
        0,
    )

    _wait_for_fork_ready(fork)
    assert get_count == 2


def test_wait_for_fork_ready_does_not_request_after_deadline(monkeypatch):
    repo = _fork_project_mock(
        target_namespace="redhat/rhel/bot-branches",
        fork_name="rhel_tests_expat",
        clone_url="https://gitlab.com/redhat/rhel/bot-branches/rhel_tests_expat.git",
    ).gitlab_repo
    get_count = 0

    class Clock:
        now = 100

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()

    def get_project(_path):
        nonlocal get_count
        get_count += 1
        clock.now = 109
        raise gitlab.GitlabGetError("503", 503, b"")

    fork = flexmock(
        namespace="redhat/rhel/bot-branches",
        repo="rhel_tests_expat",
        gitlab_repo=repo,
        service=flexmock(gitlab_instance=flexmock(projects=flexmock(get=get_project))),
    )
    monkeypatch.setattr("ymir.tools.privileged.gitlab._FORK_READY_TIMEOUT_SEC", 10)
    monkeypatch.setattr("ymir.tools.privileged.gitlab._FORK_READY_POLL_INTERVAL_SEC", 2)
    monkeypatch.setattr("ymir.tools.privileged.gitlab.time.monotonic", clock.monotonic)
    monkeypatch.setattr("ymir.tools.privileged.gitlab.time.sleep", clock.sleep)

    with pytest.raises(ToolError, match="did not become ready within"):
        _wait_for_fork_ready(fork)
    assert get_count == 1


def test_wait_for_fork_ready_wraps_gitlab_error_from_project_response():
    fork = flexmock(
        namespace="redhat/rhel/bot-branches",
        repo="rhel_tests_foo",
        service=flexmock(
            gitlab_instance=flexmock(
                projects=flexmock(
                    get=lambda _path: (_ for _ in ()).throw(gitlab.GitlabGetError("403", 403, b""))
                )
            )
        ),
    )
    with pytest.raises(ToolError, match="Failed to query fork project"):
        _wait_for_fork_ready(fork)


def test_wait_for_fork_ready_raises_on_timeout(monkeypatch):
    fork = _fork_project_mock(
        target_namespace="redhat/rhel/bot-branches",
        fork_name="rhel_tests_expat",
        clone_url="https://gitlab.com/redhat/rhel/bot-branches/rhel_tests_expat.git",
        import_status="started",
    )
    monkeypatch.setattr(
        "ymir.tools.privileged.gitlab._FORK_READY_POLL_INTERVAL_SEC",
        0,
    )
    monkeypatch.setattr(
        "ymir.tools.privileged.gitlab._FORK_READY_TIMEOUT_SEC",
        0,
    )
    with pytest.raises(ToolError, match="did not become ready"):
        _wait_for_fork_ready(fork)


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
async def test_list_project_merge_requests_requests_requested_page():
    captured_requests = []

    @asynccontextmanager
    async def fake_get(url, **kwargs):
        captured_requests.append((url, kwargs["params"]))

        async def json_response():
            return [
                {
                    "iid": 25,
                    "web_url": "https://gitlab.com/redhat/rhel/tests/shared/-/merge_requests/25",
                    "title": "shared: [RHEL-25] ymir reproducer test",
                    "state": "opened",
                }
            ]

        yield flexmock(json=json_response, raise_for_status=lambda: None, status=200)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(fake_get)

    output = await ListProjectMergeRequestsTool().run(
        input={"project": "redhat/rhel/tests/shared", "page": 2}
    )

    assert len(captured_requests) == 1
    request_url, params = captured_requests[0]
    assert request_url == "https://gitlab.com/api/v4/projects/redhat%2Frhel%2Ftests%2Fshared/merge_requests"
    assert params["page"] == "2"
    assert params["per_page"] == str(GITLAB_MR_PAGE_SIZE)
    assert output.result[0]["iid"] == 25


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

    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "git"
        assert kwargs.get("cwd") == clone_path
        if args[0] == "init":
            assert args[1:] == ("-b", "_ymir_init")
        elif args[0] == "fetch":
            assert args[1].endswith(repository.removeprefix("https://"))
            assert args[2] == f"refs/heads/{branch}:refs/heads/{branch}"
        elif args[0] == "checkout":
            assert args[1] == branch
        else:
            pytest.fail(f"Unexpected git command: {args}")

        async def _mock_communicate():
            return (b"", b"")

        return flexmock(communicate=_mock_communicate, returncode=0)

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(_mock_create_subprocess_exec)

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
    with pytest.raises(ToolError) as exc_info:
        await CloneRepositoryTool().run(
            input={"repository": "https://gitlab.com/redhat/rhel/rpms/bash", "clone_path": bad_path}
        )
    assert "must be under" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_clone_repository_rejects_path_traversal(mock_git_repo_basepath):
    traversal_path = mock_git_repo_basepath / ".." / "tmp" / "bash"
    with pytest.raises(ToolError) as exc_info:
        await CloneRepositoryTool().run(
            input={"repository": "https://gitlab.com/redhat/rhel/rpms/bash", "clone_path": traversal_path}
        )
    assert "must be under" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_clone_repository_rejects_basepath_root(mock_git_repo_basepath):
    with pytest.raises(ToolError) as exc_info:
        await CloneRepositoryTool().run(
            input={
                "repository": "https://gitlab.com/redhat/rhel/rpms/bash",
                "clone_path": mock_git_repo_basepath,
            }
        )
    assert "must be under" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_clone_repository_accepts_path_inside_basepath(mock_git_repo_basepath):
    valid_path = mock_git_repo_basepath / "RHEL-12345" / "bash"

    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        async def _mock_communicate():
            return (b"", b"")

        return flexmock(communicate=_mock_communicate, returncode=0)

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(_mock_create_subprocess_exec)

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

    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        assert cmd == "git"
        assert args[0] == "push"
        assert args[1].endswith(repository.removeprefix("https://"))
        assert args[2] == branch
        assert kwargs.get("cwd") == clone_path
        assert kwargs.get("stdout") == asyncio.subprocess.PIPE
        assert kwargs.get("stderr") == asyncio.subprocess.PIPE

        async def _mock_communicate():
            return (b"", b"")

        return flexmock(communicate=_mock_communicate, returncode=0)

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(_mock_create_subprocess_exec)
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

    assert "Could not parse merge request URL" in str(exc_info.value.__cause__)


@pytest.mark.parametrize(
    "merge_request_url, expected_project_path",
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
async def test_remove_merge_request_labels(merge_request_url, expected_project_path):
    labels = ["mr_testing_completed"]

    # Mock the raw python-gitlab object backing the ogr merge request.
    raw_pr_mock = flexmock(labels=["mr_testing_completed", "some-other-label"])
    raw_pr_mock.should_receive("save").once()

    mr_mock = flexmock(_raw_pr=raw_pr_mock)

    # Mock the project object
    project_mock = flexmock()
    project_mock.should_receive("get_pr").and_return(mr_mock)

    # Mock GitlabService.get_project_from_url
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=f"https://gitlab.com/{expected_project_path}"
    ).and_return(project_mock)

    result = (
        await RemoveMergeRequestLabelsTool().run(
            input={"merge_request_url": merge_request_url, "labels": labels}
        )
    ).result

    assert result == f"Successfully removed labels {labels} from merge request {merge_request_url}"
    assert raw_pr_mock.labels == ["some-other-label"]


@pytest.mark.asyncio
async def test_remove_merge_request_labels_label_not_present():
    merge_request_url = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123"
    labels = ["label-that-is-not-set"]

    # Mock the raw python-gitlab object backing the ogr merge request.
    raw_pr_mock = flexmock(labels=["some-other-label"])
    raw_pr_mock.should_receive("save").once()

    mr_mock = flexmock(_raw_pr=raw_pr_mock)

    project_mock = flexmock()
    project_mock.should_receive("get_pr").and_return(mr_mock)

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url="https://gitlab.com/redhat/rhel/rpms/bash"
    ).and_return(project_mock)

    result = (
        await RemoveMergeRequestLabelsTool().run(
            input={"merge_request_url": merge_request_url, "labels": labels}
        )
    ).result

    assert result == f"Successfully removed labels {labels} from merge request {merge_request_url}"
    # No-op: the label wasn't present, so nothing should have changed.
    assert raw_pr_mock.labels == ["some-other-label"]


@pytest.mark.asyncio
async def test_remove_merge_request_labels_invalid_url():
    merge_request_url = "https://github.com/user/repo/pull/123"
    labels = ["test-label"]

    with pytest.raises(Exception) as exc_info:
        await RemoveMergeRequestLabelsTool().run(
            input={"merge_request_url": merge_request_url, "labels": labels}
        )

    assert "Could not parse merge request URL" in str(exc_info.value.__cause__)


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
async def test_update_merge_request_comment():
    merge_request_url = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123"
    note_id = 456
    comment = "Updated comment text"

    note = flexmock(body=None)
    note.should_receive("save").once()

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url=merge_request_url.rsplit("/-/merge_requests/", 1)[0],
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .and_return(
            flexmock(
                id=123,
                _raw_pr=flexmock(
                    notes=flexmock().should_receive("get").with_args(note_id).and_return(note).mock(),
                ),
            ),
        )
        .mock()
    )

    result = (
        await UpdateMergeRequestCommentTool().run(
            input={"merge_request_url": merge_request_url, "note_id": note_id, "comment": comment}
        )
    ).result

    assert result == f"Successfully updated comment {note_id} on merge request {merge_request_url}"
    assert note.body == comment


@pytest.mark.asyncio
async def test_update_merge_request_comment_note_not_found():
    merge_request_url = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/123"
    note_id = 999

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
                    .should_receive("get")
                    .with_args(note_id)
                    .and_raise(gitlab.GitlabGetError("404 Not Found"))
                    .mock(),
                ),
            ),
        )
        .mock()
    )

    with pytest.raises(ToolError) as exc_info:
        await UpdateMergeRequestCommentTool().run(
            input={"merge_request_url": merge_request_url, "note_id": note_id, "comment": "x"}
        )

    assert f"Note {note_id} not found on merge request {merge_request_url}" in str(exc_info.value.__cause__)


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

    assert "Could not parse merge request URL" in str(exc_info.value.__cause__)


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

    assert "Could not parse merge request URL" in str(exc_info.value.__cause__)


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
    assert "Could not parse merge request URL" in str(exc_info.value.__cause__)


# --- sanitize_url tests ---


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "https://oauth2:glpat-abc123@gitlab.com/redhat/rpms/vim",  # pragma: allowlist secret
            "https://oauth2:***@gitlab.com/redhat/rpms/vim",
        ),
        (
            "clone failed: https://gitlab.com/redhat/rpms/vim",
            "clone failed: https://gitlab.com/redhat/rpms/vim",
        ),
        ("no credentials here", "no credentials here"),
        ("", ""),
    ],
)
def test_sanitize_url(text, expected):
    assert sanitize_url(text) == expected


# --- clone / fetch / push failure logging tests ---


def _make_failing_subprocess(stderr_text):
    """Return an async mock that simulates a git command failure with stderr."""

    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        async def _mock_communicate():
            return (b"", stderr_text.encode())

        return flexmock(communicate=_mock_communicate, returncode=128)

    return _mock_create_subprocess_exec


def _make_subprocess_sequence(results):
    """Return an async mock that yields different results per call.

    *results* is a list of (returncode, stderr) tuples, consumed in order.
    """
    call_iter = iter(results)

    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        returncode, stderr_text = next(call_iter)

        async def _mock_communicate():
            return (b"", stderr_text.encode())

        return flexmock(communicate=_mock_communicate, returncode=returncode)

    return _mock_create_subprocess_exec


@pytest.mark.asyncio
async def test_clone_repository_logs_stderr_on_failure(mock_git_repo_basepath, caplog):
    """Clone failure surfaces git stderr in logs and ToolError."""
    clone_path = mock_git_repo_basepath / "vim"
    stderr_msg = "fatal: repository 'https://github.com/vim/vim' not found"

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_subprocess_sequence(
            [
                (0, ""),  # git init succeeds
                (128, stderr_msg),  # git fetch fails
                (128, stderr_msg),  # retry 1
                (128, stderr_msg),  # retry 2
            ]
        )
    )

    async def _mock_sleep(_delay):
        return None

    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep)

    with pytest.raises(ToolError) as exc_info:
        await CloneRepositoryTool().run(
            input={
                "repository": "https://github.com/vim/vim",
                "branch": "main",
                "clone_path": clone_path,
            }
        )
    assert "not found" in str(exc_info.value.__cause__)

    assert "not found" in caplog.text
    assert "git fetch" in caplog.text
    assert "failed" in caplog.text


@pytest.mark.asyncio
async def test_clone_repository_no_branch_logs_stderr_on_failure(mock_git_repo_basepath, caplog):
    """Full clone (no branch) failure surfaces git stderr."""
    clone_path = mock_git_repo_basepath / "vim"
    stderr_msg = "fatal: unable to access: The requested URL returned error: 403"

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_failing_subprocess(stderr_msg)
    )

    with pytest.raises(ToolError) as exc_info:
        await CloneRepositoryTool().run(
            input={
                "repository": "https://github.com/vim/vim",
                "clone_path": clone_path,
            }
        )
    assert "403" in str(exc_info.value.__cause__)

    assert "403" in caplog.text
    assert "git clone" in caplog.text
    assert "failed" in caplog.text


@pytest.mark.asyncio
async def test_clone_repository_sanitizes_stderr(mock_git_repo_basepath, caplog):
    """Credentials in git stderr are sanitized before logging."""
    clone_path = mock_git_repo_basepath / "vim"
    token = "glpat-secret"  # pragma: allowlist secret
    repo = f"https://oauth2:{token}@gitlab.com/redhat/rpms/vim"
    stderr_msg = f"fatal: unable to access '{repo}/': 403"

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_failing_subprocess(stderr_msg)
    )

    with pytest.raises(ToolError) as exc_info:
        await CloneRepositoryTool().run(
            input={
                "repository": repo,
                "clone_path": clone_path,
            }
        )

    assert token not in caplog.text
    assert "oauth2:***@" in caplog.text
    assert token not in str(exc_info.value)


@pytest.mark.asyncio
async def test_clone_repository_strips_auth_header_lines_from_stderr(mock_git_repo_basepath, caplog):
    """Authorization header lines in git stderr are dropped before logging."""
    clone_path = mock_git_repo_basepath / "vim"
    stderr_msg = (
        "fatal: Authentication failed\n"
        "Authorization: Basic dG9rZW46c2VjcmV0\n"  # pragma: allowlist secret
        "remote: HTTP Basic: Access denied"
    )

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_failing_subprocess(stderr_msg)
    )

    with pytest.raises(ToolError) as exc_info:
        await CloneRepositoryTool().run(
            input={
                "repository": "https://gitlab.com/redhat/rpms/vim",
                "clone_path": clone_path,
            }
        )

    assert "Authorization: Basic" not in caplog.text
    assert "dG9rZW46c2VjcmV0" not in caplog.text
    assert "Authentication failed" in caplog.text
    assert "Authorization: Basic" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_clone_repository_removes_existing_dir_with_branch(mock_git_repo_basepath):
    """Branch-based clone removes an existing non-empty target directory first."""
    clone_path = mock_git_repo_basepath / "bash"
    clone_path.mkdir()
    (clone_path / "stale").write_text("old", encoding="utf-8")
    (clone_path / ".git").mkdir()

    commands: list[list[str]] = []

    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        commands.append([cmd, *args])

        async def _mock_communicate():
            return (b"", b"")

        process = flexmock(returncode=0)
        process.should_receive("communicate").replace_with(_mock_communicate)
        return process

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(_mock_create_subprocess_exec)

    await CloneRepositoryTool().run(
        input={
            "repository": "https://gitlab.com/centos-stream/rpms/bash",
            "branch": "rhel-8.10.0",
            "clone_path": clone_path,
        }
    )

    assert not (clone_path / "stale").exists()
    assert any(cmd[:4] == ["git", "init", "-b", "_ymir_init"] for cmd in commands)


def test_remove_existing_clone_path_tolerates_disappearing_directory(mock_git_repo_basepath):
    clone_path = mock_git_repo_basepath / "bash"
    clone_path.mkdir()

    def remove_then_raise(path):
        path.rmdir()
        raise FileNotFoundError

    flexmock(shutil).should_receive("rmtree").replace_with(remove_then_raise)
    _remove_existing_clone_path(clone_path)


def test_remove_existing_clone_path_rejects_partial_cleanup(mock_git_repo_basepath):
    clone_path = mock_git_repo_basepath / "bash"
    clone_path.mkdir()

    flexmock(shutil).should_receive("rmtree").and_raise(FileNotFoundError)
    with pytest.raises(FileNotFoundError):
        _remove_existing_clone_path(clone_path)


@pytest.mark.asyncio
async def test_fetch_branch_logs_stderr_on_failure(mock_git_repo_basepath, caplog):
    """FetchBranchTool failure surfaces git stderr."""
    clone_path = mock_git_repo_basepath / "vim"
    clone_path.mkdir()
    stderr_msg = "fatal: couldn't find remote ref main"

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_failing_subprocess(stderr_msg)
    )

    with pytest.raises(ToolError) as exc_info:
        await FetchBranchTool().run(
            input={
                "repository": "https://github.com/vim/vim",
                "branch": "main",
                "clone_path": clone_path,
            }
        )
    assert "remote ref" in str(exc_info.value.__cause__)

    assert "remote ref" in caplog.text
    assert "git fetch" in caplog.text
    assert "failed" in caplog.text


@pytest.mark.asyncio
async def test_fetch_commit_creates_namespaced_ref(mock_git_repo_basepath):
    clone_path = mock_git_repo_basepath / "vim"
    clone_path.mkdir()
    commit_sha = "a" * 40
    commands: list[list[str]] = []

    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        commands.append([cmd, *args])

        async def _mock_communicate():
            return (b"", b"")

        process = flexmock(returncode=0)
        process.should_receive("communicate").replace_with(_mock_communicate)
        return process

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(_mock_create_subprocess_exec)

    result = await FetchCommitTool().run(
        input={
            "repository": "https://gitlab.com/redhat/rhel/rpms/vim",
            "commit_sha": commit_sha,
            "clone_path": clone_path,
        }
    )

    destination = f"refs/ymir/zstream/{commit_sha}"
    assert result.result == destination
    assert any(
        command[-3:]
        == [
            "https://gitlab.com/redhat/rhel/rpms/vim",
            f"{commit_sha}:{destination}",
            "--no-tags",
        ]
        for command in commands
    )


@pytest.mark.asyncio
async def test_fetch_commit_rejects_invalid_sha(mock_git_repo_basepath):
    clone_path = mock_git_repo_basepath / "vim"
    clone_path.mkdir()

    with pytest.raises(ToolInputValidationError) as error:
        await FetchCommitTool().run(
            input={
                "repository": "https://gitlab.com/redhat/rhel/rpms/vim",
                "commit_sha": "main",
                "clone_path": clone_path,
            }
        )
    assert "commit_sha" in str(error.value.__cause__)


@pytest.mark.asyncio
async def test_get_remote_branch_head_returns_exact_ref():
    commit_sha = "a" * 40
    commands: list[list[str]] = []

    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        commands.append([cmd, *args])

        async def _mock_communicate():
            return (f"{commit_sha}\trefs/heads/automated-update\n".encode(), b"")

        process = flexmock(returncode=0)
        process.should_receive("communicate").replace_with(_mock_communicate)
        return process

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(_mock_create_subprocess_exec)

    result = await GetRemoteBranchHeadTool().run(
        input={
            "repository": "https://gitlab.com/ai-bot/vim",
            "branch": "automated-update",
        }
    )

    assert result.result == commit_sha
    assert commands[0][-4:] == [
        "ls-remote",
        "--heads",
        "https://gitlab.com/ai-bot/vim",
        "refs/heads/automated-update",
    ]


@pytest.mark.asyncio
async def test_get_remote_branch_head_rejects_missing_branch():
    async def _mock_create_subprocess_exec(cmd, *args, **kwargs):
        return process

    async def _mock_communicate():
        return (b"", b"")

    process = flexmock(returncode=0)
    process.should_receive("communicate").replace_with(_mock_communicate)
    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(_mock_create_subprocess_exec)

    with pytest.raises(ToolError) as error:
        await GetRemoteBranchHeadTool().run(
            input={
                "repository": "https://gitlab.com/ai-bot/vim",
                "branch": "automated-update",
            }
        )
    assert "Could not resolve exact head" in str(error.value)


@pytest.mark.asyncio
async def test_push_logs_stderr_on_failure(caplog):
    """PushToRemoteRepositoryTool failure surfaces git stderr."""
    clone_path = Path("/git-repos/bash")
    stderr_msg = "error: failed to push some refs to 'https://gitlab.com/ai-bot/bash.git'"

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_failing_subprocess(stderr_msg)
    )

    with pytest.raises(ToolError) as exc_info:
        await PushToRemoteRepositoryTool().run(
            input={
                "repository": "https://gitlab.com/ai-bot/bash.git",
                "clone_path": clone_path,
                "branch": "my-branch",
            }
        )
    assert "failed to push" in str(exc_info.value.__cause__)

    assert "failed to push" in caplog.text
    assert "git push" in caplog.text
    assert "failed" in caplog.text


@pytest.mark.asyncio
async def test_set_merge_request_reviewers():
    merge_request_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/1"
    reviewer_ids = [42, 99]

    raw_mr = flexmock(reviewer_ids=None)
    raw_mr.should_receive("save").once()

    mr_mock = flexmock(_raw_pr=raw_mr)

    project = flexmock(gitlab_repo=flexmock())
    (flexmock(GitlabService).should_receive("get_project_from_url").and_return(project))
    project.should_receive("get_pr").with_args(1).and_return(mr_mock)

    result = await SetMergeRequestReviewersTool().run(
        input={
            "merge_request_url": merge_request_url,
            "reviewer_ids": reviewer_ids,
        }
    )
    assert "Successfully set reviewers" in result.result
    assert raw_mr.reviewer_ids == [42, 99]


@pytest.mark.asyncio
async def test_resolve_reviewers_tool():
    async def _mock_resolve(*_args, **_kwargs):
        return [42, 99]

    flexmock(_resolver).should_receive("resolve_reviewers").replace_with(_mock_resolve)
    result = await ResolveReviewersTool().run(input={"package": "bash", "dist_git_branch": "c10s"})
    assert result.result == [42, 99]


@pytest.mark.asyncio
async def test_resolve_qe_reviewers_tool():
    async def _mock_resolve(*_args, **_kwargs):
        return [99]

    flexmock(_resolver).should_receive("resolve_qe_reviewers").replace_with(_mock_resolve)
    result = await ResolveQeReviewersTool().run(input={"package": "bash", "dist_git_branch": "c10s"})
    assert result.result == [99]


@pytest.mark.asyncio
async def test_get_merge_request_discussions():
    merge_request_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/123"

    discussions = [
        flexmock(
            id="d1",
            attributes={
                "notes": [
                    {
                        "author": {"username": "osci-bot"},
                        "body": "<!--RESULTS_COMMENT-->...",
                        "created_at": "2024-01-15T10:00:00Z",
                        "resolvable": True,
                        "resolved": False,
                    }
                ]
            },
        ),
        flexmock(
            id="d2",
            attributes={
                "notes": [
                    {
                        "author": {"username": "osci-bot"},
                        "body": "Hello, thanks for the MR",
                        "created_at": "2024-01-15T09:00:00Z",
                        "resolvable": False,
                        "resolved": None,
                    }
                ]
            },
        ),
    ]

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
                )
            )
        )
        .mock()
    )

    result = (
        await GetMergeRequestDiscussionsTool().run(input={"merge_request_url": merge_request_url})
    ).result

    assert len(result) == 2
    assert result[0].discussion_id == "d1"
    assert result[0].resolvable is True
    assert result[0].resolved is False
    assert result[0].body == "<!--RESULTS_COMMENT-->..."
    assert result[1].discussion_id == "d2"
    assert result[1].resolvable is False


@pytest.mark.asyncio
async def test_get_merge_request_commit_messages():
    merge_request_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/123"

    commits = [
        flexmock(message="Fix a thing\n\nResolves: RHEL-111\n"),
        flexmock(message=None),
        flexmock(message="Another\n\nResolves: RHEL-222\n"),
    ]

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url="https://gitlab.com/redhat/centos-stream/rpms/bash"
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .with_args(123)
        .and_return(
            flexmock(
                _raw_pr=flexmock()
                .should_receive("commits")
                .with_args(get_all=True)
                .and_return(commits)
                .mock()
            )
        )
        .mock()
    )

    result = (
        await GetMergeRequestCommitMessagesTool().run(input={"merge_request_url": merge_request_url})
    ).result

    assert result == [
        "Fix a thing\n\nResolves: RHEL-111\n",
        "",
        "Another\n\nResolves: RHEL-222\n",
    ]


@pytest.mark.asyncio
async def test_resolve_merge_request_discussion():
    merge_request_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/123"

    discussion_mock = flexmock(resolved=False)
    discussion_mock.should_receive("save").once()

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
                    .should_receive("get")
                    .with_args("d1")
                    .and_return(discussion_mock)
                    .mock()
                )
            )
        )
        .mock()
    )

    result = (
        await ResolveMergeRequestDiscussionTool().run(
            input={"merge_request_url": merge_request_url, "discussion_id": "d1"}
        )
    ).result

    assert discussion_mock.resolved is True
    assert "d1" in result


@pytest.mark.asyncio
async def test_get_pipeline_job_trace():
    merge_request_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/123"

    flexmock(GitlabService).should_receive("get_project_from_url").with_args(
        url="https://gitlab.com/redhat/centos-stream/rpms/bash"
    ).and_return(
        flexmock()
        .should_receive("get_pr")
        .with_args(123)
        .and_return(
            flexmock(
                target_project=flexmock(
                    gitlab_repo=flexmock(
                        jobs=flexmock()
                        .should_receive("get")
                        .with_args("456")
                        .and_return(
                            flexmock()
                            .should_receive("trace")
                            .and_return(
                                b"Testing Farm Result: "
                                b"https://artifacts.osci.redhat.com/testing-farm/abc-123\n"
                            )
                            .mock()
                        )
                        .mock()
                    )
                )
            )
        )
        .mock()
    )

    result = (
        await GetPipelineJobTraceTool().run(input={"merge_request_url": merge_request_url, "job_id": "456"})
    ).result

    assert "artifacts.osci.redhat.com/testing-farm/abc-123" in result


class _MergeRequestWithInaccessibleFork:
    """MR whose target project is readable but whose source (fork) project returns 404."""

    id = 4
    source_branch = "RHEL-316024"
    target_branch = "rhel-9.9.0"
    title = "Resolves: RHEL-316024"
    description = "description"
    _raw_pr = flexmock(updated_at="2026-11-26T09:00:00+00:00")
    target_project = flexmock(
        service=flexmock(user=flexmock(get_username=lambda: "bot")),
        gitlab_repo=flexmock(name="mod_security"),
    )

    @property
    def source_project(self):
        raise GitlabAPIException("404: 404 Project Not Found")


def _mock_merge_request_lookup(mr):
    async def get_mr(_url):
        return mr

    async def get_comments(_url):
        return []

    flexmock(gitlab_tool_module).should_receive("_get_merge_request_from_url").replace_with(get_mr)
    flexmock(gitlab_tool_module).should_receive(
        "_fetch_authorized_comments_from_merge_request_url"
    ).replace_with(get_comments)


@pytest.mark.asyncio
async def test_merge_request_details_fails_when_source_fork_is_inaccessible_by_default():
    _mock_merge_request_lookup(_MergeRequestWithInaccessibleFork())

    with pytest.raises(ToolError):
        await GetMergeRequestDetailsTool().run(
            input={"merge_request_url": "https://gitlab.com/redhat/rhel/rpms/mod_security/-/merge_requests/4"}
        )


@pytest.mark.asyncio
async def test_merge_request_details_returns_null_source_repo_when_flag_allows_inaccessible_fork():
    _mock_merge_request_lookup(_MergeRequestWithInaccessibleFork())

    output = await GetMergeRequestDetailsTool().run(
        input={
            "merge_request_url": "https://gitlab.com/redhat/rhel/rpms/mod_security/-/merge_requests/4",
            "allow_inaccessible_source": True,
        }
    )

    assert output.result.source_repo is None
    assert output.result.source_branch == "RHEL-316024"
    assert output.result.target_repo_name == "mod_security"
    assert output.result.comments.root == []


@pytest.mark.asyncio
async def test_merge_request_details_keeps_source_repo_when_fork_is_accessible():
    class _MergeRequestWithAccessibleFork(_MergeRequestWithInaccessibleFork):
        source_project = flexmock(
            get_git_urls=lambda: {"git": "https://gitlab.com/luhliari/mod_security.git"},
            service=flexmock(user=flexmock(get_username=lambda: "bot")),
        )

    _mock_merge_request_lookup(_MergeRequestWithAccessibleFork())

    output = await GetMergeRequestDetailsTool().run(
        input={
            "merge_request_url": "https://gitlab.com/redhat/rhel/rpms/mod_security/-/merge_requests/4",
            "allow_inaccessible_source": True,
        }
    )

    assert output.result.source_repo == "https://gitlab.com/luhliari/mod_security.git"


# ---------------------------------------------------------------------------
# GetPatchFromUrlTool / GetPatchFromUrlFullTool
# ---------------------------------------------------------------------------

PRIVATE_COMMIT_PATCH_URL = "https://gitlab.com/redhat/rhel/rpms/json-glib/-/commit/5e0be60e.patch"
PRIVATE_COMMIT_API_URL = (
    "https://gitlab.com/api/v4/projects/redhat%2Frhel%2Frpms%2Fjson-glib"
    "/repository/commits/5e0be60e/diff?per_page=100"
)
PUBLIC_PATCH_URL = "https://gitlab.gnome.org/GNOME/json-glib/-/commit/7528ac2.patch"


def _small_patch(body_lines: int = 1) -> str:
    added = "".join(f"+line {i}\n" for i in range(body_lines))
    return (
        "diff --git a/file.c b/file.c\n"
        "--- a/file.c\n"
        "+++ b/file.c\n"
        f"@@ -1,1 +1,{body_lines + 1} @@\n"
        " context\n"
        f"{added}"
    )


def _api_entry(old_path, new_path=None, diff="", a_mode="100644", b_mode="100644", **flags):
    return {
        "old_path": old_path,
        "new_path": new_path or old_path,
        "a_mode": a_mode,
        "b_mode": b_mode,
        "new_file": False,
        "renamed_file": False,
        "deleted_file": False,
        "diff": diff,
        **flags,
    }


def _diff_line_stats(body) -> dict:
    """Commit stats matching a GitLab API diff body, as GitLab computes them."""
    try:
        entries = json.loads(body)
        lines = [line for entry in entries for line in (entry.get("diff") or "").splitlines()]
    except (ValueError, TypeError, AttributeError):
        lines = []
    return {
        "additions": sum(line.startswith("+") for line in lines),
        "deletions": sum(line.startswith("-") for line in lines),
    }


def _mock_patch_get(body, status=200, headers=None, content_length=None, charset=None, stats=None):
    """Mock aiohttp GET for patch fetches; returns a list of (url, kwargs) per patch request.

    Commit stats requests are answered with ``stats``, or with stats matching ``body``.
    """
    captured: list[tuple[str, dict]] = []
    raw = body.encode() if isinstance(body, str) else body
    commit_stats = stats if stats is not None else _diff_line_stats(body)

    @asynccontextmanager
    async def fake_get(url, **kwargs):
        if re.search(r"/repository/commits/[0-9a-f]+$", url):

            async def json_response():
                return {"id": "abc", "stats": commit_stats}

            yield flexmock(status=200, raise_for_status=lambda: None, json=json_response)
            return
        captured.append((url, kwargs))

        async def text_response():
            return raw.decode()

        async def iter_chunked(size):
            for start in range(0, len(raw), size):
                yield raw[start : start + size]

        yield flexmock(
            status=status,
            headers=headers or {},
            text=text_response,
            content_length=content_length,
            charset=charset,
            content=flexmock(iter_chunked=iter_chunked),
        )

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(fake_get)
    return captured


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PRIVATE_COMMIT_PATCH_URL, PRIVATE_COMMIT_API_URL),
        (PRIVATE_COMMIT_PATCH_URL.replace("https://", "http://"), PRIVATE_COMMIT_API_URL),
        (PUBLIC_PATCH_URL, PUBLIC_PATCH_URL),
        (
            "https://gitlab.com/libssh/libssh-mirror/-/commit/abc123.patch",
            "https://gitlab.com/libssh/libssh-mirror/-/commit/abc123.patch",
        ),
        (
            "https://gitlab.com/redhat/rhel/rpms/json-glib/-/merge_requests/1.patch",
            "https://gitlab.com/redhat/rhel/rpms/json-glib/-/merge_requests/1.patch",
        ),
    ],
)
def test_get_api_diff_url(url, expected):
    assert _get_api_diff_url(url) == expected


@pytest.mark.asyncio
async def test_get_patch_from_url_preview_is_truncated():
    patch = _small_patch(body_lines=500)
    assert len(patch) > MAX_PATCH_CONTENT_LENGTH
    _mock_patch_get(patch)

    output = await GetPatchFromUrlTool().run(input={"patch_url": PUBLIC_PATCH_URL})

    assert output.result.startswith(patch[:MAX_PATCH_CONTENT_LENGTH])
    assert f"of {len(patch)} total" in output.result


@pytest.mark.asyncio
async def test_get_patch_from_url_preview_returns_error_string_on_http_error():
    _mock_patch_get("forbidden", status=403)

    output = await GetPatchFromUrlTool().run(input={"patch_url": PUBLIC_PATCH_URL})

    assert output.result == f"Error: Failed to fetch patch from {PUBLIC_PATCH_URL}: HTTP 403"


@pytest.mark.asyncio
async def test_get_patch_from_url_preview_returns_incomplete_content_without_raising(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    entries = [_api_entry("big.c", too_large=True), _api_entry("img.png", diff="Binary files differ\n")]
    _mock_patch_get(json.dumps(entries), headers={"X-Next-Page": "2"})

    output = await GetPatchFromUrlTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})

    assert "diff --git a/big.c b/big.c" in output.result


@pytest.mark.asyncio
async def test_get_patch_from_url_full_returns_untruncated_public_patch_without_token(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    patch = _small_patch(body_lines=500)
    captured = _mock_patch_get(patch)

    output = await GetPatchFromUrlFullTool().run(input={"patch_url": PUBLIC_PATCH_URL})

    assert output.result == patch
    url, kwargs = captured[0]
    assert url == PUBLIC_PATCH_URL
    assert "PRIVATE-TOKEN" not in kwargs["headers"]
    assert kwargs["allow_redirects"] is True


@pytest.mark.asyncio
async def test_get_patch_from_url_full_decodes_declared_charset():
    patch = _small_patch().replace("context", "contéxt")
    _mock_patch_get(patch.encode("latin-1"), charset="latin-1")

    output = await GetPatchFromUrlFullTool().run(input={"patch_url": PUBLIC_PATCH_URL})

    assert output.result == patch


@pytest.mark.asyncio
async def test_get_patch_from_url_full_raises_on_http_error():
    _mock_patch_get("forbidden", status=403)

    with pytest.raises(ToolError, match="HTTP 403"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PUBLIC_PATCH_URL})


@pytest.mark.asyncio
async def test_get_patch_from_url_full_rejects_non_patch_content():
    _mock_patch_get("<html><body>Sign in</body></html>")

    with pytest.raises(ToolError, match="does not look like a patch"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PUBLIC_PATCH_URL})


@pytest.mark.asyncio
async def test_get_patch_from_url_full_rejects_declared_oversized_body():
    _mock_patch_get(_small_patch(), content_length=gitlab_module.MAX_FULL_PATCH_CONTENT_LENGTH + 1)

    with pytest.raises(ToolError, match=r"exceeds the \d+ byte limit"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PUBLIC_PATCH_URL})


@pytest.mark.asyncio
async def test_get_patch_from_url_full_rejects_streamed_oversized_body(monkeypatch):
    monkeypatch.setattr(gitlab_module, "MAX_FULL_PATCH_CONTENT_LENGTH", 10)
    _mock_patch_get(_small_patch())

    with pytest.raises(ToolError, match="exceeds the 10 byte limit"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PUBLIC_PATCH_URL})


@pytest.mark.asyncio
async def test_get_patch_from_url_full_converts_private_api_diff(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    entries = [
        _api_entry(
            "0001-fix.patch",
            a_mode="0",
            new_file=True,
            diff="@@ -0,0 +1,2 @@\n+From abc\n+Subject: fix\n",
        ),
        _api_entry("json-glib.spec", diff="@@ -1,1 +1,2 @@\n Name: json-glib\n+Patch0: 0001-fix.patch\n"),
        _api_entry("old.patch", b_mode="0", deleted_file=True, diff="@@ -1,1 +0,0 @@\n-gone\n"),
        _api_entry("a.txt", "b.txt", renamed_file=True),
        _api_entry("build.sh", b_mode="100755"),
    ]
    captured = _mock_patch_get(json.dumps(entries))

    output = await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})

    url, kwargs = captured[0]
    assert url == PRIVATE_COMMIT_API_URL
    assert kwargs["headers"]["PRIVATE-TOKEN"] == "secret"
    assert kwargs["allow_redirects"] is False
    assert output.result == (
        "diff --git a/0001-fix.patch b/0001-fix.patch\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/0001-fix.patch\n"
        "@@ -0,0 +1,2 @@\n+From abc\n+Subject: fix\n"
        "diff --git a/json-glib.spec b/json-glib.spec\n"
        "--- a/json-glib.spec\n"
        "+++ b/json-glib.spec\n"
        "@@ -1,1 +1,2 @@\n Name: json-glib\n+Patch0: 0001-fix.patch\n"
        "diff --git a/old.patch b/old.patch\n"
        "deleted file mode 100644\n"
        "--- a/old.patch\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n-gone\n"
        "diff --git a/a.txt b/b.txt\n"
        "rename from a.txt\n"
        "rename to b.txt\n"
        "diff --git a/build.sh b/build.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )


@pytest.mark.asyncio
async def test_get_patch_from_url_full_converted_api_diff_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.txt").write_text("a\n")
    (repo / "old.txt").write_text("gone\n")
    (repo / "a.txt").write_text("same\n")
    (repo / "build.sh").write_text("echo hi\n")
    (repo / "both.sh").write_text("x\n")
    (repo / "nonl.txt").write_text("x")
    (repo / 'tab\tand"quote.txt').write_text("q\n")
    (repo / "old name.txt").write_text("r\n")
    for script in ("build.sh", "both.sh"):
        (repo / script).chmod(0o644)
    entries = [
        _api_entry("new.txt", a_mode="0", new_file=True, diff="@@ -0,0 +1 @@\n+new\n"),
        _api_entry("empty.txt", a_mode="0", new_file=True),
        _api_entry("mod.txt", diff="@@ -1 +1,2 @@\n a\n+b\n"),
        _api_entry("old.txt", b_mode="0", deleted_file=True, diff="@@ -1 +0,0 @@\n-gone\n"),
        _api_entry("a.txt", "b.txt", renamed_file=True),
        _api_entry("build.sh", b_mode="100755"),
        _api_entry("both.sh", b_mode="100755", diff="@@ -1 +1 @@\n-x\n+y\n"),
        _api_entry('tab\tand"quote.txt', diff="@@ -1 +1 @@\n-q\n+Q\n"),
        _api_entry("old name.txt", "new\\name.txt", renamed_file=True),
        _api_entry(
            "nonl.txt",
            diff="@@ -1 +1 @@\n-x\n\\ No newline at end of file\n+y\n\\ No newline at end of file\n",
        ),
    ]
    _mock_patch_get(json.dumps(entries))
    output = await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})

    patch_file = tmp_path / "commit.patch"
    patch_file.write_text(output.result)
    proc = await asyncio.create_subprocess_exec("git", "init", "-q", cwd=repo)
    await proc.wait()
    proc = await asyncio.create_subprocess_exec(
        "git", "apply", str(patch_file), cwd=repo, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()

    assert proc.returncode == 0, stderr.decode()
    assert (repo / "new.txt").read_text() == "new\n"
    assert (repo / "empty.txt").read_text() == ""
    assert (repo / "mod.txt").read_text() == "a\nb\n"
    assert not (repo / "old.txt").exists()
    assert not (repo / "a.txt").exists()
    assert (repo / "b.txt").read_text() == "same\n"
    assert (repo / "build.sh").stat().st_mode & 0o111
    assert (repo / "both.sh").stat().st_mode & 0o111
    assert (repo / "both.sh").read_text() == "y\n"
    assert (repo / "nonl.txt").read_text() == "y"
    assert (repo / 'tab\tand"quote.txt').read_text() == "Q\n"
    assert not (repo / "old name.txt").exists()
    assert (repo / "new\\name.txt").read_text() == "r\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["too_large", "collapsed"])
async def test_get_patch_from_url_full_rejects_omitted_api_diffs(monkeypatch, flag):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    _mock_patch_get(json.dumps([_api_entry("big.c", **{flag: True})]))

    with pytest.raises(ToolError, match=r"omitted the diff of big\.c"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})


@pytest.mark.asyncio
async def test_get_patch_from_url_full_rejects_binary_api_diffs(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    entries = [_api_entry("logo.png", diff="Binary files a/logo.png and b/logo.png differ\n")]
    _mock_patch_get(json.dumps(entries))

    with pytest.raises(ToolError, match=r"changes binary files \(logo\.png\)"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "raises"),
    [
        ({"X-Next-Page": "2"}, True),
        ({"X-Next-Page": "", "X-Prev-Page": "1"}, True),
        ({"X-Next-Page": "", "X-Prev-Page": ""}, False),
        ({"X-Next-Page": " "}, False),
    ],
)
async def test_get_patch_from_url_full_pagination(monkeypatch, headers, raises):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    entries = [_api_entry("a.c", diff="@@ -1 +1 @@\n-a\n+b\n")]
    _mock_patch_get(json.dumps(entries), headers=headers)

    if raises:
        with pytest.raises(ToolError, match="more than 100 changed files"):
            await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})
    else:
        output = await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})
        assert output.result.startswith("diff --git a/a.c b/a.c\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch_url", "body"),
    [
        # JSON that is not served by the GitLab commit diff API is never converted.
        ("https://example.com/hunks.json", json.dumps([_api_entry("a.c", diff="@@ -1 +1 @@\n-a\n+b\n")])),
        # Malformed API entries are not converted into a fake patch.
        (PRIVATE_COMMIT_PATCH_URL, "[{}]"),
        (PRIVATE_COMMIT_PATCH_URL, json.dumps([{"old_path": "a.c", "new_path": "a.c", "new_file": True}])),
        (PRIVATE_COMMIT_PATCH_URL, json.dumps([_api_entry("a.c", diff=0)])),
    ],
)
async def test_get_patch_from_url_full_does_not_convert_unrecognized_json(monkeypatch, patch_url, body):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    _mock_patch_get(body)

    with pytest.raises(ToolError, match="does not look like a patch"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": patch_url})


@pytest.mark.asyncio
async def test_authenticated_patch_fetch_upgrades_http_and_does_not_follow_redirects(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    raw_url = "http://gitlab.com/redhat/rhel/rpms/json-glib/-/raw/rhel-10.3/fix.patch"
    captured = _mock_patch_get("", status=302)

    with pytest.raises(ToolError, match="HTTP 302"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": raw_url})

    preview = await GetPatchFromUrlTool().run(input={"patch_url": raw_url})

    assert preview.result == f"Error: Failed to fetch patch from {raw_url}: HTTP 302"
    for url, kwargs in captured:
        assert url == raw_url.replace("http://", "https://")
        assert kwargs["headers"]["PRIVATE-TOKEN"] == "secret"
        assert kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_patch_fetch_sends_token_for_fork_namespace_commits(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    monkeypatch.setenv("FORK_NAMESPACE", "ymir-dev-forks")
    entries = [_api_entry("a.c", diff="@@ -1 +1 @@\n-a\n+b\n")]
    captured = _mock_patch_get(json.dumps(entries))

    await GetPatchFromUrlFullTool().run(
        input={"patch_url": "https://gitlab.com/ymir-dev-forks/json-glib/-/commit/abc123.patch"}
    )

    url, kwargs = captured[0]
    assert url == (
        "https://gitlab.com/api/v4/projects/ymir-dev-forks%2Fjson-glib/repository/commits/abc123/diff?per_page=100"
    )
    assert kwargs["headers"]["PRIVATE-TOKEN"] == "secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(("a_mode", "b_mode"), [("100644", "100644"), (None, "100644"), ("100644", None)])
async def test_get_patch_from_url_full_rejects_empty_plain_modification(monkeypatch, a_mode, b_mode):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    _mock_patch_get(json.dumps([_api_entry("a.c", a_mode=a_mode, b_mode=b_mode)]))

    with pytest.raises(ToolError, match=r"omitted the diff of a\.c"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})


@pytest.mark.asyncio
async def test_get_patch_from_url_full_rejects_partial_content():
    _mock_patch_get(_small_patch(), status=206)

    with pytest.raises(ToolError, match="HTTP 206"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PUBLIC_PATCH_URL})


@pytest.mark.asyncio
async def test_exhausted_retryable_status_is_reported(monkeypatch):
    monkeypatch.setattr(constants, "AIOHTTP_RETRY_BACKOFF_BASE", 0)
    monkeypatch.setattr(http_utils.random, "uniform", lambda *_args: 0)
    _mock_patch_get("unavailable", status=503)

    with pytest.raises(ToolError, match="HTTP 503"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PUBLIC_PATCH_URL})
    preview = await GetPatchFromUrlTool().run(input={"patch_url": PUBLIC_PATCH_URL})

    assert preview.result == f"Error: Failed to fetch patch from {PUBLIC_PATCH_URL}: HTTP 503"


@pytest.mark.asyncio
async def test_get_patch_from_url_full_checks_diff_against_commit_stats(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    entries = [_api_entry("a.c", diff="@@ -1 +1 @@\n-a\n+b\n")]
    # GitLab stopped listing files at its diff limit; the commit changes more lines.
    _mock_patch_get(json.dumps(entries), stats={"additions": 5, "deletions": 1})

    with pytest.raises(ToolError, match=r"adds 5 and removes 1 lines, but the diff adds 1"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})


@pytest.mark.asyncio
async def test_get_patch_from_url_full_rejects_converted_patch_over_limit(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    # Headers repeat each path four times, so long paths grow during conversion.
    entries = [_api_entry("d/" * 100 + f"f{i}.c", diff="@@ -1 +1 @@\n-a\n+b\n") for i in range(3)]
    body = json.dumps(entries)
    converted = GetPatchFromUrlTool._json_hunks_to_text(entries)
    assert len(converted) > len(body)
    monkeypatch.setattr(gitlab_module, "MAX_FULL_PATCH_CONTENT_LENGTH", len(body))
    _mock_patch_get(body)

    with pytest.raises(ToolError, match=rf"exceeds the {len(body)} byte limit"):
        await GetPatchFromUrlFullTool().run(input={"patch_url": PRIVATE_COMMIT_PATCH_URL})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stderr",
    [
        "error: RPC failed; curl 56 Recv failure: Connection reset by peer",
        "fetch-pack: unexpected disconnect while reading sideband packet\nfatal: early EOF",
        "fatal: unable to access 'https://github.com/vim/vim/': Could not resolve host: github.com",
    ],
)
async def test_clone_repository_retries_transient_network_failure(mock_git_repo_basepath, caplog, stderr):
    """A transient network error during fetch is retried and can succeed."""
    clone_path = mock_git_repo_basepath / "vim"

    async def _mock_sleep(_delay):
        return None

    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep)
    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_subprocess_sequence(
            [
                (0, ""),  # git init succeeds
                (128, stderr),  # git fetch fails transiently
                (0, ""),  # git fetch retry succeeds
                (0, ""),  # git checkout succeeds
            ]
        )
    )

    result = await CloneRepositoryTool().run(
        input={
            "repository": "https://github.com/vim/vim",
            "branch": "main",
            "clone_path": clone_path,
        }
    )

    assert "Successfully cloned" in result.result
    assert "retrying" in caplog.text


@pytest.mark.asyncio
async def test_clone_repository_retries_missing_ref(mock_git_repo_basepath, caplog):
    """A missing remote ref is retried too — stderr is not classified."""
    clone_path = mock_git_repo_basepath / "vim"
    stderr_msg = "fatal: couldn't find remote ref refs/heads/rhel-9.9.0"

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_subprocess_sequence(
            [
                (0, ""),  # git init succeeds
                (128, stderr_msg),  # git fetch fails
                (128, stderr_msg),  # retry 1
                (128, stderr_msg),  # retry 2
            ]
        )
    )

    async def _mock_sleep(_delay):
        return None

    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep)

    with pytest.raises(ToolError):
        await CloneRepositoryTool().run(
            input={
                "repository": "https://gitlab.com/redhat/rhel/rpms/json-glib",
                "branch": "rhel-9.9.0",
                "clone_path": clone_path,
            }
        )

    assert caplog.text.count("retrying") == gitlab_module.GIT_SUBPROCESS_MAX_RETRIES - 1


@pytest.mark.asyncio
async def test_clone_repository_exhausted_transient_retries_raise(mock_git_repo_basepath, caplog):
    """Persistent transient failures give up after the configured attempts."""

    async def _mock_sleep(_delay):
        return None

    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep)
    stderr_msg = "error: RPC failed; curl 56 Recv failure: Connection reset by peer"
    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_subprocess_sequence(
            [
                (0, ""),  # git init succeeds
                (128, stderr_msg),  # git fetch fails (attempt 1)
                (128, stderr_msg),  # git fetch fails (attempt 2)
                (128, stderr_msg),  # git fetch fails (attempt 3)
            ]
        )
    )
    clone_path = mock_git_repo_basepath / "vim"

    with pytest.raises(ToolError) as exc_info:
        await CloneRepositoryTool().run(
            input={
                "repository": "https://github.com/grafana/grafana",
                "branch": "main",
                "clone_path": clone_path,
            }
        )

    assert "Connection reset by peer" in str(exc_info.value.__cause__)


class TestRunGitCmdRetry:
    """Direct coverage of the _run_git_cmd retry loop."""

    @staticmethod
    def _mock_run_subprocess(results):
        """Mock run_subprocess to yield (returncode, stderr) tuples in order."""
        calls = []

        async def _fake(cmd, *, cwd=None, env=None):
            calls.append(cmd)
            returncode, stderr = results[len(calls) - 1]
            return (returncode, "stdout", stderr)

        flexmock(gitlab_module).should_receive("run_subprocess").replace_with(_fake)
        return calls

    @pytest.fixture(autouse=True)
    def _no_backoff_sleep(self):
        async def _mock_sleep(_delay):
            return None

        flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep)

    @pytest.mark.asyncio
    async def test_transient_failure_is_retried_and_can_succeed(self):
        stderr = "error: RPC failed; curl 56 Recv failure: Connection reset by peer"
        calls = self._mock_run_subprocess([(128, stderr), (0, "")])

        stdout = await gitlab_module._run_git_cmd(
            ["git", "fetch", "url"], label="git fetch url", retry_transient=True
        )

        assert stdout == "stdout"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_exhausted_transient_retries_raise_after_max_attempts(self):
        stderr = "fatal: early EOF"
        calls = self._mock_run_subprocess([(128, stderr), (128, stderr), (128, stderr)])

        with pytest.raises(ToolError, match="early EOF"):
            await gitlab_module._run_git_cmd(
                ["git", "fetch", "url"], label="git fetch url", retry_transient=True
            )

        assert len(calls) == gitlab_module.GIT_SUBPROCESS_MAX_RETRIES

    @pytest.mark.asyncio
    async def test_any_failure_is_retried(self):
        """Retries are not gated on stderr classification: even a local-state
        failure gets the full attempt budget before raising."""
        stderr = "fatal: couldn't find remote ref refs/heads/rhel-9.9.0"
        calls = self._mock_run_subprocess([(128, stderr), (128, stderr), (128, stderr)])

        with pytest.raises(ToolError, match="couldn't find remote ref"):
            await gitlab_module._run_git_cmd(
                ["git", "fetch", "url"], label="git fetch url", retry_transient=True
            )

        assert len(calls) == gitlab_module.GIT_SUBPROCESS_MAX_RETRIES

    @pytest.mark.asyncio
    async def test_transient_failure_is_not_retried_without_the_flag(self):
        stderr = "error: RPC failed; curl 56 Recv failure: Connection reset by peer"
        calls = self._mock_run_subprocess([(128, stderr)])

        with pytest.raises(ToolError, match="Connection reset by peer"):
            await gitlab_module._run_git_cmd(["git", "push", "url"], label="git push url")

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_clone_retry_removes_partial_destination(self, mock_git_repo_basepath):
        """A failed clone leaves a populated destination; the retry must clean it."""
        clone_path = mock_git_repo_basepath / "vim"
        clone_path.mkdir(parents=True)
        (clone_path / ".git").mkdir()  # partial clone left behind by the failed attempt
        stderr = "error: RPC failed; curl 56 Recv failure: Connection reset by peer"
        calls = self._mock_run_subprocess([(128, stderr), (0, "")])

        cleanups = []
        flexmock(gitlab_module).should_receive("_remove_existing_clone_path").replace_with(
            lambda path: cleanups.append(path)
        )

        await gitlab_module._run_git_cmd(
            ["git", "clone", "url", str(clone_path)],
            label="git clone url",
            retry_transient=True,
            retry_cleanup_path=clone_path,
        )

        assert len(calls) == 2
        assert cleanups == [clone_path]


@pytest.mark.asyncio
async def test_clone_repository_retries_http_error(mock_git_repo_basepath, caplog):
    """A denied fetch (curl 22 / HTTP 403) is still retried — the retry policy
    accepts the cost of retrying permanent failures rather than classifying
    unreliable stderr."""
    clone_path = mock_git_repo_basepath / "vim"
    stderr_msg = (
        "error: RPC failed; curl 22 The requested URL returned error: 403\n"
        "fetch-pack: unexpected disconnect while reading sideband packet"
    )

    flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
        _make_subprocess_sequence(
            [
                (0, ""),  # git init succeeds
                (128, stderr_msg),  # git fetch denied
                (128, stderr_msg),  # retry 1
                (128, stderr_msg),  # retry 2
            ]
        )
    )

    async def _mock_sleep(_delay):
        return None

    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep)

    with pytest.raises(ToolError):
        await CloneRepositoryTool().run(
            input={
                "repository": "https://gitlab.com/redhat/rhel/rpms/vim",
                "branch": "main",
                "clone_path": clone_path,
            }
        )

    assert caplog.text.count("retrying") == gitlab_module.GIT_SUBPROCESS_MAX_RETRIES - 1
