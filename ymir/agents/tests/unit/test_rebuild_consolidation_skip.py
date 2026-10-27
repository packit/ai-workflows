"""MR consolidation isn't safe for modular packages yet (the consolidation agent's
Copr build never enables a module stream in the buildroot), so the rebuild agent
must skip submitting a consolidation job for modular issues — the same guard that
already exists in the backport agent.

`run_workflow` is defined inside `rebuild_agent.main()` (unlike `backport_agent`,
where it's module-level), so it can't be called directly from a test. Instead we
drive it the same way `test_triage_agent.py` does: run `main()` in queue mode with
`run_task_loop` mocked to capture the `process_task` closure, then feed it a task
payload and intercept `Workflow.run` to jump straight to `submit_consolidation_job`.
"""

from contextlib import asynccontextmanager

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import rebuild_agent
from ymir.common.models import Task


async def _async_noop(*_args, **_kwargs):
    pass


@asynccontextmanager
async def _always_acquired_lock(*_args, **_kwargs):
    yield "test-lock-token"


def _make_payload(*, is_modular: bool) -> bytes:
    metadata = {
        "triage_result": {"data": {"package": "postgis", "jira_issue": "RHEL-999"}},
        "target_branch": "rhel-9.8.0",
    }
    if is_modular:
        # Structural signal: raw_downstream_component matches module:stream/package.
        metadata["raw_downstream_component"] = "postgresql:16/postgis"
    task = Task(metadata=metadata)
    return task.model_dump_json().encode()


async def _capture_process_task():
    """Run rebuild_agent.main() in queue mode, capture the process_task closure."""
    captured = {}

    async def _mock_run_task_loop(_redis, _queues, process_fn, **_kw):
        captured["process_task"] = process_fn

    @asynccontextmanager
    async def _mock_redis_client(*_args, **_kwargs):
        client = flexmock()
        client.should_receive("lpush").replace_with(_async_noop)
        client.should_receive("incr").replace_with(_async_noop)
        yield client

    transaction = flexmock()
    transaction.should_receive("__enter__").and_return(None)
    transaction.should_receive("__exit__").and_return(False)

    span_processor = flexmock()
    span_processor.should_receive("start_transaction").and_return(transaction)

    flexmock(rebuild_agent).should_receive("init_sentry")
    flexmock(rebuild_agent).should_receive("configure_logging")
    flexmock(rebuild_agent).should_receive("resolve_chat_model_override")
    flexmock(rebuild_agent).should_receive("setup_observability").and_return(span_processor)
    flexmock(rebuild_agent).should_receive("run_task_loop").replace_with(_mock_run_task_loop)
    flexmock(rebuild_agent).should_receive("redis_client").replace_with(_mock_redis_client)

    await rebuild_agent.main()

    return captured["process_task"]


async def _run_rebuild_task(monkeypatch, *, is_modular: bool):
    monkeypatch.setenv("COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    flexmock(rebuild_agent).should_receive("issue_lock").replace_with(_always_acquired_lock)

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    flexmock(rebuild_agent).should_receive("mcp_tools").replace_with(gateway)
    flexmock(rebuild_agent).should_receive("create_log_agent").and_return(None)
    flexmock(rebuild_agent).should_receive("get_mock_local_tool_env").and_return(None)

    run_workflow = Workflow.run

    def start_at_submit(workflow, state, options=None):
        """Skip straight to submit_consolidation_job with a successful rebuild MR."""
        if workflow.name == "RebuildWorkflow":
            state.merge_request_url = "https://gitlab.com/x/y/-/merge_requests/1"
            state.rebuild_success = True
            state.merge_request_newly_created = True
            workflow.set_start("submit_consolidation_job")
            workflow.steps["comment_in_jira"].handler = lambda _: Workflow.END
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_submit)

    process_task = await _capture_process_task()
    await process_task(_make_payload(is_modular=is_modular))


@pytest.mark.asyncio
async def test_modular_rebuild_skips_consolidation_job(monkeypatch):
    flexmock(rebuild_agent.tasks).should_receive("try_submit_consolidation_job").replace_with(
        _async_noop
    ).never()

    await _run_rebuild_task(monkeypatch, is_modular=True)


@pytest.mark.asyncio
async def test_non_modular_rebuild_submits_consolidation_job(monkeypatch):
    flexmock(rebuild_agent.tasks).should_receive("try_submit_consolidation_job").replace_with(
        _async_noop
    ).once()

    await _run_rebuild_task(monkeypatch, is_modular=False)
