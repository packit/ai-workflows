"""Modular issues must attempt a real Copr build with their module stream enabled.

There is no feature flag gating this: once a module:stream is resolved for a
modular issue, `run_build_agent` always passes it through to `BuildInputSchema`
so Copr enables the module in the chroot buildroot (`additional_modules`).
"""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import backport_agent
from ymir.common.models import BackportOutputSchema, BuildInputSchema, BuildOutputSchema


async def _run_from_build_step(monkeypatch, *, is_modular_issue, module_stream):
    """Start the real workflow at run_build_agent and capture the BuildInputSchema used."""
    captured_build_inputs = []

    async def _fake_run_build(*, build_input, **_kwargs):
        captured_build_inputs.append(build_input)
        return BuildOutputSchema(success=True, error=None)

    flexmock(backport_agent).should_receive("run_build").replace_with(_fake_run_build).once()

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway)
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None)
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    run_workflow = Workflow.run

    def start_at_build(workflow, state, options=None):
        if workflow.name == "BackportWorkflow":
            state.backport_result = BackportOutputSchema(
                success=True,
                status="Prepared SRPM",
                srpm_path=Path("/git-repos/package/package-1-1.src.rpm"),
                error=None,
            )
            workflow.set_start("run_build_agent")
            # Stop immediately after the build step routes onward.
            workflow.steps["update_release"].handler = lambda _: Workflow.END
            workflow.steps["comment_in_jira"].handler = lambda _: Workflow.END
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_build)

    await backport_agent.run_workflow(
        package="postgresql",
        dist_git_branch="c10s",
        upstream_patches=[],
        jira_issue="RHEL-999",
        cve_id=None,
        dry_run=True,
        backport_agent_factory=lambda *_: None,
        is_modular_issue=is_modular_issue,
        module_stream=module_stream,
    )

    assert len(captured_build_inputs) == 1
    return captured_build_inputs[0]


@pytest.mark.asyncio
async def test_modular_issue_attempts_build_with_module_stream_enabled(monkeypatch):
    """No flag: a resolved module:stream must always reach the Copr build call."""
    build_input = await _run_from_build_step(
        monkeypatch, is_modular_issue=True, module_stream="postgresql:16"
    )

    assert isinstance(build_input, BuildInputSchema)
    assert build_input.module_stream == "postgresql:16"


@pytest.mark.asyncio
async def test_non_modular_issue_builds_without_module_stream(monkeypatch):
    build_input = await _run_from_build_step(monkeypatch, is_modular_issue=False, module_stream=None)

    assert build_input.module_stream is None


@pytest.mark.asyncio
async def test_modular_issue_without_resolved_stream_still_builds(monkeypatch):
    """If the module:stream couldn't be parsed, the build is still attempted (just unmodularized)."""
    build_input = await _run_from_build_step(monkeypatch, is_modular_issue=True, module_stream=None)

    assert build_input.module_stream is None
