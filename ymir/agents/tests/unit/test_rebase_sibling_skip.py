"""Sibling issues must be handled like triage handles them.

A sibling exits the rebase workflow via Workflow.END before rebase_result is
set, so the task handler must treat that as a completed skip (no error retry,
no REBASE_ERRORED label). The skip is silent: the sibling marker comment
posted at triage time already tells the user the issue is deferred to the
primary, so the rebase agent comments only on errors or situations that need
the user's attention.

A sibling marker pointing at a primary that already finished (rebased,
errored, or closed) is stale: triage deliberately ignored it and queued the
issue as a primary of its own run, so the rebase must proceed instead of
skipping.
"""

from contextlib import asynccontextmanager

import pytest
from flexmock import flexmock

import ymir.agents.rebase_agent as rebase_agent
from ymir.agents import tasks
from ymir.common.models import RebaseData, Task

SIBLING_DETAILS = {
    "fields": {
        "comment": {
            "comments": [
                {"body": "Queued for triage as potential sibling of RHEL-200"},
            ]
        }
    }
}


async def _async_noop(*_args, **_kwargs):
    pass


@asynccontextmanager
async def _always_acquired_lock(*_args, **_kwargs):
    yield "test-lock-token"


@asynccontextmanager
async def _fake_mcp_tools(*_args, **_kwargs):
    yield []


def _task_payload(data, *, user_triggered: bool = False) -> bytes:
    task = Task.from_issue(data.jira_issue, user_triggered=user_triggered)
    task.metadata["target_branch"] = "rhel-10.3"
    task.metadata["triage_result"] = {"data": data.model_dump()}
    return task.model_dump_json().encode()


def _prepare_common_mocks(monkeypatch, payload, process_results):
    monkeypatch.setenv("COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://mcp-gateway:8000/sse")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("CONTAINER_VERSION", "c10s")
    monkeypatch.setenv("MAX_RETRIES", "3")

    transaction = flexmock()
    transaction.should_receive("__enter__").and_return(None)
    transaction.should_receive("__exit__").and_return(False)
    span_processor = flexmock()
    span_processor.should_receive("start_transaction").and_return(transaction)

    async def _mock_run_task_loop(_redis, _queues, process_fn, **_kwargs):
        process_results.append(await process_fn(payload))

    redis_calls = []
    redis_mock = flexmock()

    async def _record_lpush(key, value):
        redis_calls.append((key, value))

    async def _mock_incr(_key):
        return 1

    redis_mock.should_receive("lpush").replace_with(_record_lpush)
    redis_mock.should_receive("incr").replace_with(_mock_incr)

    @asynccontextmanager
    async def _mock_redis_client(*_args, **_kwargs):
        yield redis_mock

    flexmock(rebase_agent).should_receive("init_sentry")
    flexmock(rebase_agent).should_receive("configure_logging")
    flexmock(rebase_agent).should_receive("resolve_chat_model_override")
    flexmock(rebase_agent).should_receive("setup_observability").and_return(span_processor)
    flexmock(rebase_agent).should_receive("run_task_loop").replace_with(_mock_run_task_loop)
    flexmock(rebase_agent).should_receive("redis_client").replace_with(_mock_redis_client)
    flexmock(rebase_agent).should_receive("issue_lock").replace_with(_always_acquired_lock)
    flexmock(rebase_agent).should_receive("mcp_tools").replace_with(_fake_mcp_tools)
    flexmock(rebase_agent).should_receive("create_log_agent").and_return(flexmock())
    flexmock(rebase_agent).should_receive("create_rebase_agent").and_return(flexmock())

    async def _sibling_details(*_args, **_kwargs):
        return SIBLING_DETAILS

    flexmock(rebase_agent).should_receive("run_tool").replace_with(_sibling_details)

    async def _no_siblings(*_args, **_kwargs):
        return [], None

    flexmock(rebase_agent).should_receive("find_triaged_rebase_siblings").replace_with(_no_siblings)

    labels = []
    comments = []

    async def _record_labels(**kwargs):
        labels.append(kwargs)

    async def _record_comment(**kwargs):
        comments.append(kwargs)

    flexmock(tasks).should_receive("set_jira_labels").replace_with(_record_labels)
    flexmock(tasks).should_receive("comment_in_jira").replace_with(_record_comment)
    flexmock(tasks).should_receive("post_terminal_error_comment").replace_with(_record_comment)
    flexmock(tasks).should_receive("change_jira_status").replace_with(_async_noop)

    return labels, comments, redis_calls


@pytest.mark.asyncio
async def test_live_sibling_link_skips_silently_and_completes_task(monkeypatch):
    """A live sibling link skips the rebase without commenting.

    The sibling marker comment from triage already documents the deferral, so
    the skip must not add another comment.
    """
    payload = _task_payload(RebaseData(package="postgresql16", version="16.16", jira_issue="RHEL-99999"))
    process_results = []
    labels, comments, redis_calls = _prepare_common_mocks(monkeypatch, payload, process_results)

    async def _not_stale(*_args, **_kwargs):
        return False

    flexmock(rebase_agent).should_receive("is_sibling_link_stale").replace_with(_not_stale)

    await rebase_agent.main()

    # Task completed without error: no requeue result, no error-list push.
    assert process_results == [None]
    assert redis_calls == []
    # The skip is silent: no comment, no label transitions.
    assert comments == []
    assert labels == []


@pytest.mark.asyncio
async def test_stale_sibling_link_proceeds_with_rebase(monkeypatch):
    """A marker naming a finished primary is stale: the rebase must proceed.

    Triage already ignored the stale marker and dispatched this issue as a
    primary, so skipping here would defer the rebase forever. The workflow
    must move past the sibling check; a sentinel failure in the next real step
    proves it got there.
    """
    payload = _task_payload(RebaseData(package="postgresql16", version="16.16", jira_issue="RHEL-99999"))
    process_results = []
    _labels, comments, redis_calls = _prepare_common_mocks(monkeypatch, payload, process_results)

    async def _stale(*_args, **_kwargs):
        return True

    flexmock(rebase_agent).should_receive("is_sibling_link_stale").replace_with(_stale)

    sentinel = RuntimeError("proceeded-past-sibling-check")

    async def _raise_sentinel(*_args, **_kwargs):
        raise sentinel

    flexmock(tasks).should_receive("fork_and_prepare_dist_git").replace_with(_raise_sentinel)

    await rebase_agent.main()

    # No comments at all: the workflow proceeded past the sibling check and
    # failed later at dist-git preparation (the sentinel), which went through
    # the regular requeue path instead — no comment is posted on a first
    # retryable attempt.
    assert comments == []
    # The task was requeued through the regular retry path, not completed as a skip.
    assert [(key, value) for key, value in redis_calls if "RHEL-99999" in str(value)]


@pytest.mark.asyncio
async def test_unreadable_primary_retries_on_automatic_run(monkeypatch):
    """An unreadable primary on an automatic run must reach the retry path.

    Triage dispatched this rebase deliberately; a transient Jira outage must
    not complete the task as a sibling skip, which would park the rebase
    forever.
    """
    payload = _task_payload(RebaseData(package="postgresql16", version="16.16", jira_issue="RHEL-99999"))
    process_results = []
    labels, comments, redis_calls = _prepare_common_mocks(monkeypatch, payload, process_results)

    async def _unreadable(*_args, **_kwargs):
        raise RuntimeError("jira unavailable")

    flexmock(rebase_agent).should_receive("is_sibling_link_stale").replace_with(_unreadable)

    await rebase_agent.main()

    # The task was requeued through the regular retry path, not completed as a skip.
    assert [(key, value) for key, value in redis_calls if "RHEL-99999" in str(value)]
    assert comments == []
    assert labels == []


@pytest.mark.asyncio
async def test_unreadable_primary_proceeds_on_user_triggered_run(monkeypatch):
    """Matching triage: an explicit request must not silently defer."""
    payload = _task_payload(
        RebaseData(package="postgresql16", version="16.16", jira_issue="RHEL-99999"),
        user_triggered=True,
    )
    process_results = []
    _labels, comments, redis_calls = _prepare_common_mocks(monkeypatch, payload, process_results)

    async def _unreadable(*_args, **_kwargs):
        raise RuntimeError("jira unavailable")

    flexmock(rebase_agent).should_receive("is_sibling_link_stale").replace_with(_unreadable)

    sentinel = RuntimeError("proceeded-past-sibling-check")

    async def _raise_sentinel(*_args, **_kwargs):
        raise sentinel

    flexmock(tasks).should_receive("fork_and_prepare_dist_git").replace_with(_raise_sentinel)

    await rebase_agent.main()

    assert comments == []
    assert [(key, value) for key, value in redis_calls if "RHEL-99999" in str(value)]
