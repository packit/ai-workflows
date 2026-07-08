from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ymir.agents.triage_agent import _should_update_jira
from ymir.common.models import Resolution, Task


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.REBASE,
        Resolution.BACKPORT,
        Resolution.REBUILD,
        Resolution.NOT_AFFECTED,
        Resolution.POSTPONED,
        Resolution.OPEN_ENDED_ANALYSIS,
        Resolution.CLARIFICATION_NEEDED,
        Resolution.ERROR,
    ],
)
def test_user_triggered_always_posts(resolution):
    """A maintainer-triggered run always gets a comment, regardless of resolution."""
    assert _should_update_jira(resolution=resolution, user_triggered=True) is True


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.REBASE,
        Resolution.BACKPORT,
        Resolution.REBUILD,
    ],
)
def test_non_user_triggered_skips_comment_when_mr_will_be_opened(resolution):
    """Without ymir_todo, runs do not comment when an MR will be opened —
    the MR itself is the user-visible artifact."""
    assert _should_update_jira(resolution=resolution, user_triggered=False) is False


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.NOT_AFFECTED,
        Resolution.POSTPONED,
        Resolution.OPEN_ENDED_ANALYSIS,
        Resolution.CLARIFICATION_NEEDED,
    ],
)
def test_non_user_triggered_still_posts_when_no_mr_will_open(resolution):
    """Resolutions that do not produce an MR must still post a comment —
    otherwise the result is invisible to the requester."""
    assert _should_update_jira(resolution=resolution, user_triggered=False) is True


def test_non_user_triggered_error_does_not_post():
    """ERROR is handled by separate error-path machinery, not this helper."""
    assert _should_update_jira(resolution=Resolution.ERROR, user_triggered=False) is False


def _make_payload(issue: str = "RHEL-99999", user_triggered: bool = False) -> bytes:
    task = Task.from_issue(issue, user_triggered=user_triggered)
    return task.model_dump_json().encode()


async def _capture_process_task(main_fn):
    """Run main() in queue mode, capture the process_task closure it registers."""
    captured = {}

    async def fake_run_task_loop(_redis, _queues, process_fn, **_kw):
        captured["process_task"] = process_fn

    with (
        patch("ymir.agents.triage_agent.init_sentry"),
        patch("ymir.agents.triage_agent.configure_logging"),
        patch("ymir.agents.triage_agent.resolve_chat_model_override"),
        patch("ymir.agents.triage_agent.setup_observability", return_value=MagicMock()),
        patch("ymir.agents.triage_agent.run_task_loop", side_effect=fake_run_task_loop),
        patch("ymir.agents.triage_agent.redis_client") as mock_redis_ctx,
        patch.dict(
            "os.environ",
            {
                "COLLECTOR_ENDPOINT": "http://localhost:6006",
                "REDIS_URL": "redis://localhost",
                "MCP_GATEWAY_URL": "http://mcp-gateway:8000/sse",
                "DRY_RUN": "true",
            },
            clear=False,
        ),
    ):
        mock_redis_ctx.return_value.__aenter__ = AsyncMock()
        mock_redis_ctx.return_value.__aexit__ = AsyncMock()
        await main_fn()

    return captured["process_task"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Closed", "Done"])
async def test_process_task_skips_closed_issues(status):
    """Closed/Done issues are skipped without calling run_workflow."""
    from ymir.agents.triage_agent import main

    process_task = await _capture_process_task(main)

    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([], status),
        ),
        patch("ymir.agents.triage_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
    ):
        await process_task(_make_payload())

    mock_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_task_skips_closed_user_triggered_with_cleanup():
    """User-triggered run on a closed issue removes ymir_todo and posts ack."""
    from ymir.agents.triage_agent import main

    process_task = await _capture_process_task(main)

    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=(["ymir_todo"], "Closed"),
        ),
        patch("ymir.agents.triage_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock) as mock_labels,
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock) as mock_ack,
    ):
        await process_task(_make_payload(user_triggered=True))

    mock_workflow.assert_not_awaited()
    mock_labels.assert_awaited_once()
    _, kwargs = mock_labels.call_args
    assert kwargs["labels_to_remove"] == ["ymir_todo"]
    assert kwargs["dry_run"] is True
    mock_ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_task_proceeds_for_open_issues():
    """An open issue (e.g. New) is not blocked by the closed-issue check."""
    from ymir.agents.triage_agent import main

    process_task = await _capture_process_task(main)

    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([], "New"),
        ),
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock),
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock),
        patch("ymir.agents.triage_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
    ):
        await process_task(_make_payload())

    mock_workflow.assert_awaited_once()
