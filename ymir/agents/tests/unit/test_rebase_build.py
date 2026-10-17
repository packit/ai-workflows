"""Konflux build path for the rebase agent.

Under the Konflux backend the rebase build step must push the Ymir-owned fork
update branch and hand the build tool git_url/revision/package_name/target_branch
(a git ref) rather than only an SRPM. When a pristine base is known it first
soft-resets so the provisional build commit stays collapsible into one MR commit.
"""

import contextlib
import json
from pathlib import Path

import pytest
from beeai_framework.tools.mcp import MCPTool
from beeai_framework.workflows import Workflow
from flexmock import flexmock
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPToolInfo

from ymir.agents import rebase_agent
from ymir.common.models import RebaseOutputSchema


def _konflux_build_tool(session):
    # Mirror the real Konflux build_package schema (konflux.py): the four ref
    # fields are mandatory strings, jira_issue is optional, and
    # additionalProperties is true so the COPR-only fields pass through.
    return MCPTool(
        session,
        MCPToolInfo(
            name="build_package",
            inputSchema={
                "type": "object",
                "properties": {
                    "git_url": {"type": "string"},
                    "revision": {"type": "string"},
                    "package_name": {"type": "string"},
                    "target_branch": {"type": "string"},
                    "jira_issue": {"type": "string"},
                },
                "required": ["git_url", "revision", "package_name", "target_branch"],
                "additionalProperties": True,
            },
        ),
    )


@pytest.mark.parametrize("has_base_head", [True, False])
@pytest.mark.asyncio
async def test_konflux_build_pushes_ref_and_populates_build_fields(monkeypatch, has_base_head):
    package = "package"
    fork_url = "https://fork.invalid/package.git"
    update_branch = "rebase-update"
    base_head = "0000000000000000000000000000000000000000"  # pragma: allowlist secret
    revision = "abc123def456abc123def456abc123def456abcd"  # pragma: allowlist secret

    monkeypatch.setenv("BUILD_BACKEND", "konflux")

    call_tool_args = []

    async def _mock_call_tool(*_args, **_kwargs):
        call_tool_args.append((_args, _kwargs))
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"success": True}))])

    _mock_session = flexmock()
    _mock_session.should_receive("call_tool").replace_with(_mock_call_tool)
    tool = _konflux_build_tool(_mock_session)

    @contextlib.asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield [tool]

    flexmock(rebase_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(rebase_agent).should_receive("create_rebase_agent").and_return(flexmock())
    flexmock(rebase_agent).should_receive("create_log_agent").and_return(flexmock())
    flexmock(rebase_agent).should_receive("get_mock_local_tool_env").and_return(None)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    async def _mock_stage_changes(*_args, **_kwargs):
        return None

    async def _mock_commit_changes(*_args, **_kwargs):
        return revision

    push_calls = []

    async def _mock_push_changes(*args, **_kwargs):
        push_calls.append(args)
        return

    monkeypatch.setattr(rebase_agent.tasks, "stage_changes", _mock_stage_changes)
    monkeypatch.setattr(rebase_agent.tasks, "commit_changes", _mock_commit_changes)
    monkeypatch.setattr(rebase_agent.tasks, "push_changes", _mock_push_changes)

    check_calls = []

    async def _mock_check_subprocess(cmd, *_args, **_kwargs):
        check_calls.append(cmd)
        return ("", "")

    monkeypatch.setattr(rebase_agent, "check_subprocess", _mock_check_subprocess)

    run_workflow = Workflow.run
    completed = []

    def stop(state):
        completed.append(state)
        return Workflow.END

    def start_at_build(workflow, state, options=None):
        if workflow.name == "RebaseWorkflow":
            state.local_clone = Path("/git-repos/package")
            state.fork_url = fork_url
            state.update_branch = update_branch
            state.spec_name = "renamed.spec"
            state.rebase_base_head = base_head if has_base_head else None
            state.all_files_git_to_add = set()
            state.rebase_result = RebaseOutputSchema(
                success=True,
                status="Rebased",
                srpm_path=Path("/git-repos/package/package-2-1.src.rpm"),
                files_to_git_add=None,
                error=None,
            )
            workflow.set_start("run_build_agent")
            workflow.steps["update_release"].handler = stop
            workflow.steps["comment_in_jira"].handler = stop
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_build)

    state = await rebase_agent.run_workflow(
        package=package,
        dist_git_branch="c10s",
        version="2",
        jira_issue="RHEL-12345",
        dry_run=True,
    )

    assert len(completed) == 1, "Build validation did not reach its success route"

    # The Ymir-owned fork update branch is pushed before the build is submitted.
    assert len(push_calls) == 1
    _local, pushed_fork, pushed_branch, _tools = push_calls[0]
    assert pushed_fork == fork_url
    assert pushed_branch == update_branch

    # The pristine base is soft-reset only when known.
    soft_resets = [c for c in check_calls if c[:3] == ["git", "reset", "--soft"]]
    assert len(soft_resets) == (1 if has_base_head else 0)
    if has_base_head:
        assert soft_resets[0] == ["git", "reset", "--soft", base_head]

    # The build tool receives the git ref, not just an SRPM.
    assert len(call_tool_args) == 1
    _, build_kwargs = call_tool_args[0]
    assert build_kwargs["arguments"] == {
        "srpm_path": "/git-repos/package/package-2-1.src.rpm",
        "dist_git_branch": "c10s",
        "jira_issue": "RHEL-12345",
        "git_url": fork_url,
        "revision": revision,
        "package_name": package,
        "target_branch": "c10s",
        "specfile": "renamed.spec",
    }
    assert state.rebase_result.success is True
