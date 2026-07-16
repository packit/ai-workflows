from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ymir.agents.triage_agent import (
    _is_modular,
    _map_version_to_module_branch,
    _parse_module_summary,
    _should_update_jira,
)
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


# --- Modular detection tests ---


@pytest.mark.parametrize(
    "summary, expected",
    [
        ("postgresql:12/postgresql:PostgreSQL: Arbitrary code execution", True),
        ("postgresql:12.0/postgresql:PostgreSQL: some vulnerability", True),
        ("nodejs:18/nodejs:Node.js: buffer overflow", True),
        ("perl-DBD-MySQL:8.0/perl-DBD-MySQL:Fix for crash", True),
        ("ruby:3.1-beta/ruby:Ruby: CVE fix", True),
        ("python3.11:3.11/python3.11:Python: CVE fix", True),
        ("gcc-c++:10/gcc-c++:GCC: CVE fix", True),
        ("postgresql:PostgreSQL: Arbitrary code execution", False),
        ("some plain summary without colons", False),
        ("", False),
        (None, False),
    ],
)
def test_is_modular(summary, expected):
    assert _is_modular(summary) is expected


# --- Module summary parsing tests ---


@pytest.mark.parametrize(
    "summary, expected_module, expected_stream",
    [
        ("postgresql:12/postgresql:PostgreSQL: vuln", "postgresql", "12"),
        ("nodejs:18/nodejs:Node.js: issue", "nodejs", "18"),
        ("perl-DBD-MySQL:8.0/perl-DBD-MySQL:Fix", "perl-DBD-MySQL", "8.0"),
        ("ruby:3.1-beta/ruby:Ruby: CVE", "ruby", "3.1-beta"),
        ("python3.11:3.11/python3.11:Python: CVE", "python3.11", "3.11"),
        ("gcc-c++:10/gcc-c++:GCC: CVE", "gcc-c++", "10"),
    ],
)
def test_parse_module_summary(summary, expected_module, expected_stream):
    result = _parse_module_summary(summary)
    assert result is not None
    module, stream = result
    assert module == expected_module
    assert stream == expected_stream


def test_parse_module_summary_non_modular():
    assert _parse_module_summary("postgresql:PostgreSQL: vuln") is None


# --- Modular branch mapping tests ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version, summary, expected_branch",
    [
        (
            "rhel-9.8",
            "postgresql:12/postgresql:PostgreSQL: vuln",
            "stream-postgresql-12-rhel-9.8.0",
        ),
        (
            "rhel-9.9",
            "postgresql:12/postgresql:PostgreSQL: vuln",
            "stream-postgresql-12-rhel-9.9.0",
        ),
        (
            "rhel-10.2",
            "nodejs:18/nodejs:Node.js: issue",
            "stream-nodejs-18-rhel-10.2.0",
        ),
        (
            "rhel-9.8.z",
            "postgresql:12/postgresql:PostgreSQL: vuln",
            "stream-postgresql-12-rhel-9.8.0",
        ),
    ],
)
async def test_map_version_to_module_branch(version, summary, expected_branch):
    branch = await _map_version_to_module_branch(version, summary, cve_needs_internal_fix=False)
    assert branch == expected_branch


@pytest.mark.asyncio
async def test_map_version_to_module_branch_invalid_version():
    branch = await _map_version_to_module_branch(
        "not-a-version", "postgresql:12/postgresql:vuln", cve_needs_internal_fix=False
    )
    assert branch is None
