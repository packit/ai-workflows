"""Konflux backend reorders commit/push to happen BEFORE the build.

Copr (default) builds from a local SRPM and pushes only after a green build;
Konflux builds from a pushed git ref, so the backport workflow must commit and
push to the fork, build from that ref, and open the MR only afterwards. These
tests drive the real workflow routing (via ``set_start`` + handler overrides,
mirroring test_backport_build_logs.py) and assert the ordering.
"""

from contextlib import asynccontextmanager

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import backport_agent
from ymir.agents import tasks as agent_tasks
from ymir.common.models import BackportOutputSchema, BuildOutputSchema, LogOutputSchema


def _prepare_common_mocks(monkeypatch):
    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    async def _no_zstream_label(*args, **kwargs):
        return False

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway).once()
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None).once()
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None).once()
    flexmock(agent_tasks).should_receive("needs_zstream_target_label").replace_with(_no_zstream_label)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")


def _base_state(state, *, local_clone):
    state.local_clone = local_clone
    state.fork_url = "https://fork.example/repo.git"
    state.update_branch = "ymir-RHEL-123"
    state.used_cherry_pick_workflow = False
    state.backport_log = ["Backported the fix"]
    state.log_result = LogOutputSchema(title="Fix the thing", description="Backport of the fix")
    state.backport_result = BackportOutputSchema(
        success=True,
        status="Backported",
        srpm_path=local_clone / "expat.src.rpm",
        error=None,
    )


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.asyncio
async def test_konflux_commits_and_pushes_before_build(monkeypatch, tmp_path, dry_run):
    monkeypatch.setenv("BUILD_BACKEND", "konflux")
    local_clone = tmp_path / "expat"
    local_clone.mkdir()
    calls = []

    async def _commit(_clone, _message, *a, **k):
        calls.append("commit")
        return "a" * 40

    async def _push(_clone, _fork, _branch, _tools, *a, **k):
        calls.append("push")

    async def _build(**kwargs):
        calls.append("build")
        bi = kwargs["build_input"]
        assert bi.git_url == "https://fork.example/repo.git"
        assert bi.revision == "a" * 40
        assert bi.package_name == "expat"
        return BuildOutputSchema(success=True, error=None)

    async def _open_mr(**kwargs):
        calls.append("open_mr")
        return "https://mr.example/1", True

    flexmock(agent_tasks).should_receive("commit_changes").replace_with(_commit).once()
    flexmock(agent_tasks).should_receive("push_changes").replace_with(_push).once()
    flexmock(backport_agent).should_receive("run_build").replace_with(_build).once()
    flexmock(agent_tasks).should_receive("open_update_merge_request").replace_with(_open_mr).times(
        0 if dry_run else 1
    )
    _prepare_common_mocks(monkeypatch)

    run_workflow = Workflow.run

    def start_at(workflow, state, options=None):
        _base_state(state, local_clone=local_clone)
        workflow.set_start("konflux_build_and_publish")
        workflow.steps["submit_consolidation_job"].handler = lambda _: Workflow.END
        workflow.steps["comment_in_jira"].handler = lambda _: Workflow.END
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at)
    state = await backport_agent.run_workflow(
        package="expat",
        dist_git_branch="c10s",
        upstream_patches=["https://example/patch.patch"],
        jira_issue="RHEL-123",
        cve_id=None,
        dry_run=dry_run,
        backport_agent_factory=lambda *_: None,
    )

    assert state.backport_result.success
    # Commit + push always precede the build (Konflux builds from the ref).
    assert calls.index("commit") < calls.index("build")
    assert calls.index("push") < calls.index("build")
    if dry_run:
        # Pushed so Konflux can build, but no MR is opened in dry-run.
        assert "open_mr" not in calls
    else:
        # The MR opens only after a green build.
        assert calls.index("build") < calls.index("open_mr")


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.asyncio
async def test_konflux_inherit_build_validates_pushed_commit(monkeypatch, tmp_path, dry_run):
    monkeypatch.setenv("BUILD_BACKEND", "konflux")
    local_clone = tmp_path / "expat"
    local_clone.mkdir()
    calls = []

    async def _build(**kwargs):
        calls.append("build")
        bi = kwargs["build_input"]
        assert bi.git_url == "https://fork.example/repo.git"
        assert bi.revision == "b" * 40
        return BuildOutputSchema(success=True, error=None)

    flexmock(backport_agent).should_receive("run_build").replace_with(_build).once()
    _prepare_common_mocks(monkeypatch)

    run_workflow = Workflow.run

    def start_at(workflow, state, options=None):
        _base_state(state, local_clone=local_clone)
        state.inherit_local_commit = "b" * 40
        state.inherit_build_attempts = 3
        workflow.set_start("konflux_inherit_build")
        workflow.steps["open_inherited_mr"].handler = lambda _: (calls.append("open_mr"), Workflow.END)[1]
        workflow.steps["submit_consolidation_job"].handler = lambda _: (
            calls.append("submit"),
            Workflow.END,
        )[1]
        workflow.steps["comment_in_jira"].handler = lambda _: Workflow.END
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

    assert "build" in calls
    if dry_run:
        assert "submit" in calls and "open_mr" not in calls
    else:
        assert "open_mr" in calls
