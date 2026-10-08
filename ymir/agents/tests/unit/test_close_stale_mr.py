"""On a rerun the agents close a lingering open MR before re-pushing.

Re-pushing the update branch would otherwise re-trigger GitLab CI (scratch
builds) on the stale MR. The backport workflow therefore routes
``change_jira_status`` -> ``close_stale_merge_requests`` -> ``fork_and_prepare_dist_git``.
The close is a GitLab write, so it is suppressed under dry-run. These tests drive
the real workflow routing (via ``set_start`` + handler overrides, mirroring
test_konflux_reorder.py).
"""

from contextlib import asynccontextmanager

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import backport_agent
from ymir.agents import tasks as agent_tasks


def _prepare_common_mocks(monkeypatch):
    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway).once()
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None).once()
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None).once()
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.asyncio
async def test_close_stale_merge_requests_runs_before_fork(monkeypatch, tmp_path, dry_run):
    calls = []

    async def _close(**kwargs):
        calls.append("close")
        assert kwargs["jira_issue"] == "RHEL-123"
        assert kwargs["package"] == "expat"
        assert kwargs["dist_git_branch"] == "c10s"
        return ["https://mr.example/old"]

    flexmock(agent_tasks).should_receive("close_stale_update_merge_requests").replace_with(_close).times(
        0 if dry_run else 1
    )
    _prepare_common_mocks(monkeypatch)

    run_workflow = Workflow.run

    def start_at(workflow, state, options=None):
        workflow.set_start("close_stale_merge_requests")
        workflow.steps["fork_and_prepare_dist_git"].handler = lambda _: (
            calls.append("fork"),
            Workflow.END,
        )[1]
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at)
    await backport_agent.run_workflow(
        package="expat",
        dist_git_branch="c10s",
        upstream_patches=["https://example/patch.patch"],
        jira_issue="RHEL-123",
        cve_id=None,
        dry_run=dry_run,
        backport_agent_factory=lambda *_: None,
    )

    assert "fork" in calls
    if dry_run:
        assert "close" not in calls
    else:
        assert calls.index("close") < calls.index("fork")
