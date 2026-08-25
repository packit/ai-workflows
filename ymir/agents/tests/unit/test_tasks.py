from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from ymir.agents.tasks import (
    InvalidReleaseBumpingConfigError,
    ZStreamBranchStaleError,
    _check_zstream_branch_consistency,
    change_jira_status,
    commit_push_and_open_mr,
    fetch_release_bumping_config,
    fork_and_prepare_dist_git,
    get_jira_issue_metadata,
    handle_zstream_branch_stale_error,
    needs_zstream_target_label,
    post_user_ack_once,
    request_mr_qe_reviews,
)
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import Task


@asynccontextmanager
async def _fake_mcp_tools(_url, **_kwargs):
    yield []


def _make_task(metadata: dict | None = None, attempts: int = 0) -> Task:
    return Task(metadata=metadata or {"issue": "RHEL-1"}, attempts=attempts, user_triggered=True)


@pytest.fixture(autouse=True)
def _mcp_url_env(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://mcp-gateway:8000/sse")


@pytest.fixture
def git_repo_basepath(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_fork_and_prepare_dist_git_wipes_stale_working_dir(git_repo_basepath):
    """Re-running for the same JIRA issue must remove the previous working directory."""
    jira_issue = "RHEL-12345"
    package = "some-package"
    branch = "rhel-10.0"
    agent_type = "Rebase"

    working_dir = git_repo_basepath / agent_type / jira_issue
    working_dir.mkdir(parents=True)
    stale_file = working_dir / "leftover-artifact.txt"
    stale_file.write_text("stale")

    mock_tools = [AsyncMock()]

    with (
        patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run_tool,
        patch("ymir.agents.tasks.check_subprocess", new_callable=AsyncMock),
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
        patch("ymir.agents.tasks._check_zstream_branch_consistency", new_callable=AsyncMock),
    ):
        mock_run_tool.return_value = "https://fork.example.com"

        await fork_and_prepare_dist_git(
            jira_issue=jira_issue,
            package=package,
            dist_git_branch=branch,
            available_tools=mock_tools,
            agent_type=agent_type,
        )

    assert working_dir.is_dir(), "working_dir should be recreated"
    assert not stale_file.exists(), "stale artifacts from previous run should be gone"


@pytest.mark.asyncio
async def test_fork_and_prepare_honors_explicit_centos_stream_namespace(git_repo_basepath):
    """Modular stream-* branches must use the explicit namespace, not is_cs_branch."""
    mock_tools = [AsyncMock()]
    with (
        patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run_tool,
        patch("ymir.agents.tasks.check_subprocess", new_callable=AsyncMock),
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
    ):
        mock_run_tool.return_value = "https://fork.example.com"

        await fork_and_prepare_dist_git(
            jira_issue="RHEL-160675",
            package="squid",
            dist_git_branch="stream-squid-4-rhel-8.10.0",
            available_tools=mock_tools,
            agent_type="Rebase",
            dist_git_namespace="centos-stream",
        )

    fork_call = mock_run_tool.await_args_list[0]
    assert fork_call.args[0] == "fork_repository"
    assert fork_call.kwargs["repository"] == "https://gitlab.com/redhat/centos-stream/rpms/squid"

    tool_names = [call.args[0] for call in mock_run_tool.await_args_list]
    assert "create_zstream_branch" not in tool_names


@pytest.mark.asyncio
async def test_fork_and_prepare_modular_rhel_skips_create_zstream_branch(git_repo_basepath):
    mock_tools = [AsyncMock()]
    with (
        patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run_tool,
        patch("ymir.agents.tasks.check_subprocess", new_callable=AsyncMock),
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
    ):
        mock_run_tool.return_value = "https://fork.example.com"

        await fork_and_prepare_dist_git(
            jira_issue="RHEL-160675",
            package="squid",
            dist_git_branch="stream-squid-4-rhel-8.10.0",
            available_tools=mock_tools,
            agent_type="Rebase",
            dist_git_namespace="rhel",
        )

    fork_call = mock_run_tool.await_args_list[0]
    assert fork_call.kwargs["repository"] == "https://gitlab.com/redhat/rhel/rpms/squid"
    tool_names = [call.args[0] for call in mock_run_tool.await_args_list]
    assert "create_zstream_branch" not in tool_names


@pytest.mark.asyncio
async def test_post_user_ack_once_posts_on_first_call():
    """User-triggered, not dry-run, never posted → posts and persists the flag."""
    task = _make_task()
    with (
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
        patch("ymir.agents.tasks.comment_in_jira", new_callable=AsyncMock) as mock_comment,
    ):
        await post_user_ack_once(
            task=task,
            jira_issue="RHEL-1",
            agent_type="Triage",
            comment_text="hello",
            user_triggered=True,
            dry_run=False,
        )

    mock_comment.assert_awaited_once()
    assert task.metadata["ack_posted"] is True


@pytest.mark.asyncio
async def test_post_user_ack_once_skips_when_already_posted():
    """Second call with the same task must not re-post — even after re-queue."""
    task = _make_task(metadata={"issue": "RHEL-1", "ack_posted": True})
    with (
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
        patch("ymir.agents.tasks.comment_in_jira", new_callable=AsyncMock) as mock_comment,
    ):
        await post_user_ack_once(
            task=task,
            jira_issue="RHEL-1",
            agent_type="Triage",
            comment_text="hello",
            user_triggered=True,
            dry_run=False,
        )

    mock_comment.assert_not_awaited()
    assert task.metadata["ack_posted"] is True


@pytest.mark.asyncio
async def test_post_user_ack_once_skips_when_not_user_triggered():
    task = _make_task()
    with (
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
        patch("ymir.agents.tasks.comment_in_jira", new_callable=AsyncMock) as mock_comment,
    ):
        await post_user_ack_once(
            task=task,
            jira_issue="RHEL-1",
            agent_type="Triage",
            comment_text="hello",
            user_triggered=False,
            dry_run=False,
        )

    mock_comment.assert_not_awaited()
    assert "ack_posted" not in task.metadata


@pytest.mark.asyncio
async def test_post_user_ack_once_skips_on_dry_run():
    task = _make_task()
    with (
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
        patch("ymir.agents.tasks.comment_in_jira", new_callable=AsyncMock) as mock_comment,
    ):
        await post_user_ack_once(
            task=task,
            jira_issue="RHEL-1",
            agent_type="Triage",
            comment_text="hello",
            user_triggered=True,
            dry_run=True,
        )

    mock_comment.assert_not_awaited()
    assert "ack_posted" not in task.metadata


@pytest.mark.asyncio
async def test_change_jira_status_skips_when_flag_unset(monkeypatch):
    """Default behavior: JIRA_ALLOW_STATUS_CHANGES unset → no MCP call."""
    monkeypatch.delenv("JIRA_ALLOW_STATUS_CHANGES", raising=False)
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run_tool:
        await change_jira_status("RHEL-1", "In Progress", available_tools=[])
    mock_run_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_jira_status_skips_when_flag_false(monkeypatch):
    monkeypatch.setenv("JIRA_ALLOW_STATUS_CHANGES", "false")
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run_tool:
        await change_jira_status("RHEL-1", "In Progress", available_tools=[])
    mock_run_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_jira_status_runs_when_flag_true(monkeypatch):
    monkeypatch.setenv("JIRA_ALLOW_STATUS_CHANGES", "true")
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run_tool:
        await change_jira_status("RHEL-1", "In Progress", available_tools=[])
    mock_run_tool.assert_awaited_once()
    # The MCP tool is called with the expected arguments
    _, kwargs = mock_run_tool.call_args
    assert kwargs["issue_key"] == "RHEL-1"
    assert kwargs["status"] == "In Progress"


@pytest.mark.asyncio
async def test_post_user_ack_once_does_not_persist_on_failure():
    """On post failure, ack_posted stays unset so the next retry can try again."""
    task = _make_task()
    with (
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
        patch(
            "ymir.agents.tasks.comment_in_jira",
            new_callable=AsyncMock,
            side_effect=RuntimeError("jira down"),
        ) as mock_comment,
    ):
        # Must swallow the exception (caller relies on this)
        await post_user_ack_once(
            task=task,
            jira_issue="RHEL-1",
            agent_type="Triage",
            comment_text="hello",
            user_triggered=True,
            dry_run=False,
        )

    mock_comment.assert_awaited_once()
    assert "ack_posted" not in task.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_name, expected_status",
    [
        ("Closed", "Closed"),
        ("Done", "Done"),
        ("In Progress", "In Progress"),
        ("New", "New"),
    ],
)
async def test_get_jira_issue_metadata_returns_labels_and_status(status_name, expected_status):
    """get_jira_issue_metadata extracts both labels and status from one API call."""
    fake_details = {
        "fields": {
            "labels": ["ymir_todo", "SecurityTracking"],
            "status": {"name": status_name},
        }
    }
    with (
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
        patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock, return_value=fake_details),
    ):
        labels, status = await get_jira_issue_metadata("RHEL-99999")

    assert labels == ["ymir_todo", "SecurityTracking"]
    assert status == expected_status


@pytest.mark.asyncio
async def test_get_jira_issue_metadata_returns_defaults_on_failure():
    """On MCP/network failure, return empty labels and None status."""
    with (
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
        patch(
            "ymir.agents.tasks.run_tool",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connection refused"),
        ),
    ):
        labels, status = await get_jira_issue_metadata("RHEL-99999")

    assert labels == []
    assert status is None


MOCK_RHEL_CONFIG = {
    "current_y_streams": {"9": "rhel-9.9", "10": "rhel-10.3"},
    "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.8.z", "10": "rhel-10.2.z"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch, fix_version, expected",
    [
        ("c10s", "rhel-10.0.z", True),
        ("c9s", "rhel-9.7.z", True),
        ("c10s", "rhel-10.1", False),
        ("c9s", None, False),
        ("rhel-9.7.0", "rhel-9.7.z", False),
        ("c10s", "rhel-9.0.0.z", True),
        ("c8s", "rhel-8.10.z", False),
    ],
)
async def test_needs_zstream_target_label(branch, fix_version, expected):
    async def _mock_config():
        return MOCK_RHEL_CONFIG

    with patch("ymir.agents.tasks.load_rhel_config", _mock_config):
        assert await needs_zstream_target_label(branch, fix_version) == expected


@pytest.mark.asyncio
async def test_commit_push_and_open_mr_assigns_reviewers(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSIGN_MR_REVIEWERS", "true")
    tool_calls = []

    async def mock_run_tool(name, *, available_tools=None, **kwargs):
        tool_calls.append((name, kwargs))
        if name == "open_merge_request":
            return {"url": "https://gitlab.com/redhat/rpms/bash/-/merge_requests/1", "is_new_mr": True}
        if name == "resolve_reviewers":
            return [42, 99]
        return None

    with (
        patch("ymir.agents.tasks.commit_and_push", new_callable=AsyncMock, return_value=True),
        patch("ymir.agents.tasks.run_tool", side_effect=mock_run_tool),
    ):
        url, is_new = await commit_push_and_open_mr(
            local_clone=tmp_path,
            commit_message="test",
            fork_url="https://gitlab.com/bot/bash.git",
            dist_git_branch="c10s",
            update_branch="automated-package-update-RHEL-1",
            mr_title="Fix RHEL-1",
            mr_description="desc",
            available_tools=[],
            package="bash",
        )

    assert url is not None
    assert is_new is True
    reviewer_calls = [(n, kw) for n, kw in tool_calls if n == "set_merge_request_reviewers"]
    assert len(reviewer_calls) == 1
    assert reviewer_calls[0][1]["reviewer_ids"] == [42, 99]


@pytest.mark.asyncio
async def test_request_mr_qe_reviews_assigns_qe_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSIGN_MR_REVIEWERS", "true")
    tool_calls = []

    async def mock_run_tool(name, *, available_tools=None, **kwargs):
        tool_calls.append((name, kwargs))
        if name == "resolve_qe_reviewers":
            return [99]
        return None

    with patch("ymir.agents.tasks.run_tool", side_effect=mock_run_tool):
        await request_mr_qe_reviews(
            "bind",
            "c10s",
            "https://gitlab.com/redhat/rhel/tests/bind/-/merge_requests/1",
            [],
        )

    assert tool_calls[0][0] == "resolve_qe_reviewers"
    assert tool_calls[0][1] == {"package": "bind", "dist_git_branch": "c10s"}
    reviewer_calls = [(n, kw) for n, kw in tool_calls if n == "set_merge_request_reviewers"]
    assert len(reviewer_calls) == 1
    assert reviewer_calls[0][1]["reviewer_ids"] == [99]


@pytest.mark.asyncio
async def test_commit_push_and_open_mr_reviewer_failure_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSIGN_MR_REVIEWERS", "true")

    async def mock_run_tool(name, *, available_tools=None, **kwargs):
        if name == "open_merge_request":
            return {"url": "https://gitlab.com/redhat/rpms/bash/-/merge_requests/1", "is_new_mr": True}
        if name == "resolve_reviewers":
            return [42]
        if name == "set_merge_request_reviewers":
            raise RuntimeError("GitLab API down")
        return None

    with (
        patch("ymir.agents.tasks.commit_and_push", new_callable=AsyncMock, return_value=True),
        patch("ymir.agents.tasks.run_tool", side_effect=mock_run_tool),
    ):
        url, is_new = await commit_push_and_open_mr(
            local_clone=tmp_path,
            commit_message="test",
            fork_url="https://gitlab.com/bot/bash.git",
            dist_git_branch="c10s",
            update_branch="automated-package-update-RHEL-1",
            mr_title="Fix RHEL-1",
            mr_description="desc",
            available_tools=[],
            package="bash",
        )

    assert url is not None
    assert is_new is True


@pytest.mark.asyncio
async def test_commit_push_and_open_mr_no_reviewers_on_reused_mr(tmp_path):
    tool_calls = []

    async def mock_run_tool(name, *, available_tools=None, **kwargs):
        tool_calls.append((name, kwargs))
        if name == "open_merge_request":
            return {"url": "https://gitlab.com/redhat/rpms/bash/-/merge_requests/1", "is_new_mr": False}
        return None

    with (
        patch("ymir.agents.tasks.commit_and_push", new_callable=AsyncMock, return_value=True),
        patch("ymir.agents.tasks.run_tool", side_effect=mock_run_tool),
    ):
        url, is_new = await commit_push_and_open_mr(
            local_clone=tmp_path,
            commit_message="test",
            fork_url="https://gitlab.com/bot/bash.git",
            dist_git_branch="c10s",
            update_branch="automated-package-update-RHEL-1",
            mr_title="Fix RHEL-1",
            mr_description="desc",
            available_tools=[],
            package="bash",
        )

    assert url is not None
    assert is_new is False
    reviewer_calls = [n for n, _ in tool_calls if n == "set_merge_request_reviewers"]
    assert len(reviewer_calls) == 0


@pytest.mark.asyncio
async def test_commit_push_and_open_mr_no_reviewers_without_package(tmp_path):
    tool_calls = []

    async def mock_run_tool(name, *, available_tools=None, **kwargs):
        tool_calls.append((name, kwargs))
        if name == "open_merge_request":
            return {"url": "https://gitlab.com/redhat/rpms/bash/-/merge_requests/1", "is_new_mr": True}
        return None

    with (
        patch("ymir.agents.tasks.commit_and_push", new_callable=AsyncMock, return_value=True),
        patch("ymir.agents.tasks.run_tool", side_effect=mock_run_tool),
    ):
        url, is_new = await commit_push_and_open_mr(
            local_clone=tmp_path,
            commit_message="test",
            fork_url="https://gitlab.com/bot/bash.git",
            dist_git_branch="c10s",
            update_branch="automated-package-update-RHEL-1",
            mr_title="Fix RHEL-1",
            mr_description="desc",
            available_tools=[],
        )

    assert url is not None
    assert is_new is True
    reviewer_calls = [n for n, _ in tool_calls if n == "set_merge_request_reviewers"]
    assert len(reviewer_calls) == 0


@pytest.mark.asyncio
async def test_zstream_consistency_stale_not_ancestor(tmp_path):
    """Branch HEAD does not contain the build ref (exit 1) -> stale."""
    with (
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
        patch(
            "ymir.agents.tasks.get_latest_candidate_build",
            new_callable=AsyncMock,
            return_value=("1.0-1", "build-ref-sha"),
        ),
        patch(
            "ymir.agents.tasks.run_subprocess",
            new_callable=AsyncMock,
            side_effect=[
                (1, None, None),  # merge-base --is-ancestor
                (0, "branch-head-sha\n", None),  # rev-parse HEAD
            ],
        ),
        pytest.raises(ZStreamBranchStaleError) as exc_info,
    ):
        await _check_zstream_branch_consistency("golang", "rhel-9.8.0", tmp_path)

    assert exc_info.value.package == "golang"
    assert exc_info.value.branch == "rhel-9.8.0"
    assert exc_info.value.build_ref == "build-ref-sha"
    assert exc_info.value.branch_head == "branch-head-sha"


@pytest.mark.asyncio
async def test_zstream_consistency_stale_ref_not_in_repo(tmp_path):
    """Build ref missing from clone (exit 128) -> stale."""
    with (
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
        patch(
            "ymir.agents.tasks.get_latest_candidate_build",
            new_callable=AsyncMock,
            return_value=("1.0-1", "missing-build-ref"),
        ),
        patch(
            "ymir.agents.tasks.run_subprocess",
            new_callable=AsyncMock,
            side_effect=[
                (128, None, "fatal: Not a valid commit name missing-build-ref"),
                (0, "branch-head-sha\n", None),
            ],
        ),
        pytest.raises(ZStreamBranchStaleError),
    ):
        await _check_zstream_branch_consistency("golang", "rhel-9.8.0", tmp_path)


@pytest.mark.asyncio
async def test_zstream_consistency_up_to_date(tmp_path):
    """Build ref is ancestor of HEAD (exit 0) -> no error."""
    with (
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
        patch(
            "ymir.agents.tasks.get_latest_candidate_build",
            new_callable=AsyncMock,
            return_value=("1.0-1", "build-ref-sha"),
        ),
        patch(
            "ymir.agents.tasks.run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, None, None),
        ) as mock_run,
    ):
        await _check_zstream_branch_consistency("golang", "rhel-9.8.0", tmp_path)

    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_zstream_consistency_skips_non_zstream(tmp_path):
    """CentOS Stream branches skip the check entirely."""
    with (
        patch("ymir.agents.tasks.get_latest_candidate_build", new_callable=AsyncMock) as mock_brew,
        patch("ymir.agents.tasks.get_latest_z_pending_build", new_callable=AsyncMock) as mock_pending,
    ):
        await _check_zstream_branch_consistency("bash", "c10s", tmp_path)

    mock_brew.assert_not_awaited()
    mock_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_zstream_consistency_brew_unreachable_soft_fails(tmp_path, caplog):
    """Brew query failure logs a warning and does not raise."""
    with (
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
        patch(
            "ymir.agents.tasks.get_latest_candidate_build",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Brew unreachable"),
        ),
        patch("ymir.agents.tasks.run_subprocess", new_callable=AsyncMock) as mock_run,
    ):
        await _check_zstream_branch_consistency("golang", "rhel-9.8.0", tmp_path)

    mock_run.assert_not_awaited()
    assert "Could not query Brew" in caplog.text


@pytest.mark.asyncio
async def test_zstream_consistency_older_uses_z_pending(tmp_path):
    """Older z-streams query z-pending, not candidate."""
    with (
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=True),
        patch(
            "ymir.agents.tasks.get_latest_z_pending_build",
            new_callable=AsyncMock,
            return_value=("1.0-1", "build-ref-sha"),
        ) as mock_pending,
        patch(
            "ymir.agents.tasks.get_latest_candidate_build",
            new_callable=AsyncMock,
        ) as mock_candidate,
        patch(
            "ymir.agents.tasks.run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, None, None),
        ),
    ):
        await _check_zstream_branch_consistency("bash", "rhel-9.6.0", tmp_path)

    mock_pending.assert_awaited_once_with("bash", "rhel-9.6.0")
    mock_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_zstream_consistency_unexpected_git_exit_soft_fails(tmp_path, caplog):
    """Unexpected merge-base exit codes log a warning and do not raise."""
    with (
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
        patch(
            "ymir.agents.tasks.get_latest_candidate_build",
            new_callable=AsyncMock,
            return_value=("1.0-1", "build-ref-sha"),
        ),
        patch(
            "ymir.agents.tasks.run_subprocess",
            new_callable=AsyncMock,
            return_value=(2, None, "fatal: not a git repository"),
        ) as mock_run,
    ):
        await _check_zstream_branch_consistency("golang", "rhel-9.8.0", tmp_path)

    mock_run.assert_awaited_once()
    assert "Unexpected git merge-base exit 2" in caplog.text


@pytest.mark.asyncio
async def test_handle_zstream_branch_stale_error_labels_comments_and_error_list():
    exc = ZStreamBranchStaleError("golang", "rhel-9.8.0", "build-ref-sha", "branch-head-sha")
    redis = AsyncMock()
    redis.lpush = AsyncMock()

    with (
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock) as mock_labels,
        patch("ymir.agents.tasks.comment_in_jira", new_callable=AsyncMock) as mock_comment,
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
    ):
        await handle_zstream_branch_stale_error(
            exc,
            jira_issues=["RHEL-1", "RHEL-2", "RHEL-1"],
            primary_jira_issue="RHEL-1",
            agent_type="Rebuild",
            errored_label=JiraLabels.REBUILD_ERRORED.value,
            triaged_label=JiraLabels.TRIAGED_REBUILD.value,
            dry_run=False,
            user_triggered=False,
            redis_conn=redis,
        )

    assert mock_labels.await_count == 2
    assert mock_comment.await_count == 2
    mock_comment.assert_any_await(
        jira_issue="RHEL-1",
        agent_type="Rebuild",
        comment_text=str(exc),
        available_tools=[],
        is_error=True,
        user_triggered=True,
    )
    redis.lpush.assert_awaited_once()
    assert redis.lpush.await_args.args[0] == RedisQueues.ERROR_LIST.value


@pytest.mark.asyncio
async def test_handle_zstream_branch_stale_error_skips_comment_on_dry_run():
    exc = ZStreamBranchStaleError("golang", "rhel-9.8.0", "build-ref-sha", "branch-head-sha")
    redis = AsyncMock()
    redis.lpush = AsyncMock()

    with (
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock) as mock_labels,
        patch("ymir.agents.tasks.comment_in_jira", new_callable=AsyncMock) as mock_comment,
        patch("ymir.agents.tasks.mcp_tools", _fake_mcp_tools),
    ):
        await handle_zstream_branch_stale_error(
            exc,
            jira_issues=["RHEL-1"],
            primary_jira_issue="RHEL-1",
            agent_type="Rebase",
            errored_label=JiraLabels.REBASE_ERRORED.value,
            triaged_label=JiraLabels.TRIAGED_REBASE.value,
            dry_run=True,
            user_triggered=False,
            redis_conn=redis,
        )

    mock_labels.assert_awaited_once()
    mock_comment.assert_not_awaited()
    redis.lpush.assert_awaited_once()


# -- fetch_release_bumping_config ---------------------------------------------


@pytest.mark.asyncio
async def test_fetch_release_bumping_config_returns_default_when_not_found():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "No maintainer rules found for package 'bash' (file 'ymir.yaml' not found)"
        config = await fetch_release_bumping_config("bash", [])

    assert config.abandon_autorelease is False
    assert config.treat_maintenance_rhel_as_zstream is False
    assert config.disregard_zstream_nvr_policy is False


@pytest.mark.asyncio
async def test_fetch_release_bumping_config_parses_valid_yaml():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            "release_bumping:\n"
            "  abandon_autorelease: true\n"
            "  treat_maintenance_rhel_as_zstream: true\n"
            "  disregard_zstream_nvr_policy: true\n"
        )
        config = await fetch_release_bumping_config("bash", [])

    assert config.abandon_autorelease is True
    assert config.treat_maintenance_rhel_as_zstream is True
    assert config.disregard_zstream_nvr_policy is True


@pytest.mark.asyncio
async def test_fetch_release_bumping_config_returns_default_on_exception():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = RuntimeError("network error")
        config = await fetch_release_bumping_config("bash", [])

    assert config.abandon_autorelease is False
    assert config.treat_maintenance_rhel_as_zstream is False
    assert config.disregard_zstream_nvr_policy is False


@pytest.mark.asyncio
async def test_fetch_release_bumping_config_raises_on_malformed_section():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "release_bumping:\n  abandon_autorelease: not_a_bool\n"
        with pytest.raises(InvalidReleaseBumpingConfigError, match="malformed"):
            await fetch_release_bumping_config("bash", [])


@pytest.mark.asyncio
async def test_fetch_release_bumping_config_raises_on_invalid_yaml_syntax():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "release_bumping:\n  abandon_autorelease: [\n"
        with pytest.raises(InvalidReleaseBumpingConfigError, match="not valid YAML"):
            await fetch_release_bumping_config("bash", [])


@pytest.mark.asyncio
async def test_fetch_release_bumping_config_returns_default_when_no_release_bumping_key():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "some_other_setting: true\n"
        config = await fetch_release_bumping_config("bash", [])

    assert config.abandon_autorelease is False
    assert config.treat_maintenance_rhel_as_zstream is False
    assert config.disregard_zstream_nvr_policy is False
