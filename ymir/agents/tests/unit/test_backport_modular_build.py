"""Modular issues must attempt a real Copr build with their module stream enabled.

There is no feature flag gating this: once a module:stream is resolved for a
modular issue, the build step always passes it through to `BuildInputSchema`
so Copr enables the module in the chroot buildroot (`additional_modules`).
"""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import backport_agent, tasks
from ymir.common.models import BackportOutputSchema, BuildInputSchema, BuildOutputSchema, LogOutputSchema


async def _run_from_build_step(
    monkeypatch,
    *,
    is_modular_issue,
    module_stream,
    backend=None,
    dist_git_branch="c10s",
    copr_rhel_majors=None,
):
    """Start the real workflow at commit_push_and_build and capture the BuildInputSchema used."""
    captured_build_inputs = []
    captured_tool_names = []
    if copr_rhel_majors is None:
        monkeypatch.delenv("COPR_RHEL_MAJORS", raising=False)
    else:
        monkeypatch.setenv("COPR_RHEL_MAJORS", copr_rhel_majors)

    async def _fake_run_build(*, build_input, tool_name="build_package", **_kwargs):
        captured_build_inputs.append(build_input)
        captured_tool_names.append(tool_name)
        return BuildOutputSchema(success=True, error=None)

    async def _fake_commit_changes(*_args, **_kwargs):
        return "abc123"

    async def _no_zstream_label(*_args, **_kwargs):
        return False

    flexmock(backport_agent).should_receive("run_build").replace_with(_fake_run_build).once()
    flexmock(tasks).should_receive("commit_changes").replace_with(_fake_commit_changes).once()
    flexmock(tasks).should_receive("needs_zstream_target_label").replace_with(_no_zstream_label)

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway)
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None)
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")
    if backend is None:
        monkeypatch.delenv("BUILD_BACKEND", raising=False)
    else:
        monkeypatch.setenv("BUILD_BACKEND", backend)

    run_workflow = Workflow.run

    def start_at_build(workflow, state, options=None):
        if workflow.name == "BackportWorkflow":
            state.backport_result = BackportOutputSchema(
                success=True,
                status="Prepared SRPM",
                srpm_path=Path("/git-repos/package/package-1-1.src.rpm"),
                error=None,
            )
            state.log_result = LogOutputSchema(title="title", description="description")
            state.backport_log = ["Prepared SRPM"]
            state.local_clone = Path("/git-repos/package")
            state.fork_url = "https://example.invalid/fork.git"
            state.update_branch = "ymir-update"
            state.spec_name = "postgresql.spec"
            workflow.set_start("commit_push_and_build")
            # Stop immediately after the build step routes onward.
            workflow.steps["submit_consolidation_job"].handler = lambda _: Workflow.END
            workflow.steps["comment_in_jira"].handler = lambda _: Workflow.END
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_build)

    await backport_agent.run_workflow(
        package="postgresql",
        dist_git_branch=dist_git_branch,
        upstream_patches=[],
        jira_issue="RHEL-999",
        cve_id=None,
        dry_run=True,
        backport_agent_factory=lambda *_: None,
        is_modular_issue=is_modular_issue,
        module_stream=module_stream,
    )

    assert len(captured_build_inputs) == 1
    return captured_build_inputs[0], captured_tool_names[0]


@pytest.mark.asyncio
async def test_modular_issue_attempts_build_with_module_stream_enabled(monkeypatch):
    """No flag: a resolved module:stream must always reach the Copr build call."""
    build_input, tool_name = await _run_from_build_step(
        monkeypatch, is_modular_issue=True, module_stream="postgresql:16"
    )

    assert isinstance(build_input, BuildInputSchema)
    assert build_input.module_stream == "postgresql:16"
    assert tool_name == "build_package"


@pytest.mark.asyncio
async def test_non_modular_issue_builds_without_module_stream(monkeypatch):
    build_input, tool_name = await _run_from_build_step(
        monkeypatch, is_modular_issue=False, module_stream=None
    )

    assert build_input.module_stream is None
    assert tool_name == "build_package"


@pytest.mark.asyncio
async def test_modular_issue_without_resolved_stream_still_builds(monkeypatch):
    """If the module:stream couldn't be parsed, the build is still attempted (just unmodularized)."""
    build_input, tool_name = await _run_from_build_step(
        monkeypatch, is_modular_issue=True, module_stream=None
    )

    assert build_input.module_stream is None
    assert tool_name == "build_package"


@pytest.mark.asyncio
async def test_modular_issue_stays_on_copr_when_backend_is_konflux(monkeypatch):
    """Konflux cannot enable a module stream, so modular builds call the Copr tool."""
    build_input, tool_name = await _run_from_build_step(
        monkeypatch,
        is_modular_issue=True,
        module_stream="postgresql:16",
        backend="konflux",
    )

    assert build_input.module_stream == "postgresql:16"
    assert tool_name == "build_package_copr"


@pytest.mark.parametrize(
    "dist_git_branch,copr_rhel_majors,expected_tool",
    [
        ("rhel-8.10.0", None, "build_package_copr"),
        ("rhel-8.10.0", "", "build_package"),
        ("rhel-9.6.0", None, "build_package"),
        ("rhel-9.6.0", "8,9", "build_package_copr"),
    ],
)
@pytest.mark.asyncio
async def test_copr_rhel_majors_select_copr_when_backend_is_konflux(
    monkeypatch, dist_git_branch, copr_rhel_majors, expected_tool
):
    """Branches whose RHEL major is listed in COPR_RHEL_MAJORS (default 8) build with Copr."""
    fake_scratch_calls = []

    @asynccontextmanager
    async def _fake_scratch_build_branch(*args, **_kwargs):
        fake_scratch_calls.append(args)
        yield

    flexmock(tasks).should_receive("scratch_build_branch").replace_with(_fake_scratch_build_branch)

    _build_input, tool_name = await _run_from_build_step(
        monkeypatch,
        is_modular_issue=False,
        module_stream=None,
        backend="konflux",
        dist_git_branch=dist_git_branch,
        copr_rhel_majors=copr_rhel_majors,
    )

    assert tool_name == expected_tool
    assert bool(fake_scratch_calls) is (expected_tool == "build_package")
