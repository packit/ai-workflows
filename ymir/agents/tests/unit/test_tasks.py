from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from ymir.agents.tasks import change_jira_status, fork_and_prepare_dist_git, post_user_ack_once
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
