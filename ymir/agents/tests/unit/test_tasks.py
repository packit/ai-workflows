from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from ymir.agents.tasks import (
    change_jira_status,
    commit_push_and_open_mr,
    fork_and_prepare_dist_git,
    get_jira_issue_metadata,
    needs_zstream_target_label,
    post_user_ack_once,
)
from ymir.common.models import Task


@asynccontextmanager
async def _fake_mcp_tools(_url):
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

    working_dir = git_repo_basepath / jira_issue
    working_dir.mkdir()
    stale_file = working_dir / "leftover-artifact.txt"
    stale_file.write_text("stale")

    mock_tools = [AsyncMock()]

    with (
        patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run_tool,
        patch("ymir.agents.tasks.check_subprocess", new_callable=AsyncMock),
        patch("ymir.agents.tasks.is_older_zstream", new_callable=AsyncMock, return_value=False),
    ):
        mock_run_tool.return_value = "https://fork.example.com"

        await fork_and_prepare_dist_git(
            jira_issue=jira_issue,
            package=package,
            dist_git_branch=branch,
            available_tools=mock_tools,
        )

    assert working_dir.is_dir(), "working_dir should be recreated"
    assert not stale_file.exists(), "stale artifacts from previous run should be gone"


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
        return None

    with (
        patch("ymir.agents.tasks.commit_and_push", new_callable=AsyncMock, return_value=True),
        patch("ymir.agents.tasks.run_tool", side_effect=mock_run_tool),
        patch(
            "ymir.agents.tasks.resolve_reviewers",
            new_callable=AsyncMock,
            return_value=[42, 99],
        ),
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
async def test_commit_push_and_open_mr_reviewer_failure_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSIGN_MR_REVIEWERS", "true")

    async def mock_run_tool(name, *, available_tools=None, **kwargs):
        if name == "open_merge_request":
            return {"url": "https://gitlab.com/redhat/rpms/bash/-/merge_requests/1", "is_new_mr": True}
        if name == "set_merge_request_reviewers":
            raise RuntimeError("GitLab API down")
        return None

    with (
        patch("ymir.agents.tasks.commit_and_push", new_callable=AsyncMock, return_value=True),
        patch("ymir.agents.tasks.run_tool", side_effect=mock_run_tool),
        patch(
            "ymir.agents.tasks.resolve_reviewers",
            new_callable=AsyncMock,
            return_value=[42],
        ),
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
