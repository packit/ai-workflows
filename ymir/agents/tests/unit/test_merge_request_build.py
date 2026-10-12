"""Konflux build path for the merge-request agent.

The MR agent's update_branch is the live source branch of an existing MR, so the
Konflux build must never push an unvalidated commit there. Instead it commits the
working tree, pushes a dedicated throwaway scratch branch for the build, then
soft-resets so the normal stage -> commit_and_push flow runs unchanged (still
honouring commit_only=dry_run on the real branch).
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

from ymir.agents import merge_request_agent
from ymir.common.models import MergeRequestOutputSchema


def _konflux_build_tool(session):
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


@pytest.mark.asyncio
async def test_konflux_build_pushes_scratch_branch_and_populates_build_fields(monkeypatch):
    package = "package"
    fork_url = "https://fork.invalid/package.git"
    update_branch = "mr-source-branch"
    base_sha = "0000000000000000000000000000000000000000"  # pragma: allowlist secret
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

    flexmock(merge_request_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(merge_request_agent).should_receive("create_merge_request_agent").and_return(flexmock())
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    async def _mock_stage_changes(*_args, **_kwargs):
        return None

    async def _mock_commit_changes(*_args, **_kwargs):
        return revision

    push_calls = []

    async def _mock_push_changes(*args, **_kwargs):
        push_calls.append(args)
        return

    monkeypatch.setattr(merge_request_agent.tasks, "stage_changes", _mock_stage_changes)
    monkeypatch.setattr(merge_request_agent.tasks, "commit_changes", _mock_commit_changes)
    monkeypatch.setattr(merge_request_agent.tasks, "push_changes", _mock_push_changes)

    check_calls = []

    async def _mock_check_subprocess(cmd, *_args, **_kwargs):
        check_calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return (f"{base_sha}\n", "")
        return ("", "")

    monkeypatch.setattr(merge_request_agent, "check_subprocess", _mock_check_subprocess)

    run_workflow = Workflow.run
    completed = []

    def stop(state):
        completed.append(state)
        return Workflow.END

    def start_at_build(workflow, state, options=None):
        if workflow.name == "MergeRequestWorkflow":
            state.local_clone = Path("/git-repos/package")
            state.fork_url = fork_url
            state.update_branch = update_branch
            state.jira_issue = "RHEL-12345"
            state.package = package
            state.dist_git_branch = "c10s"
            state.all_files_git_to_add = set()
            state.mr_update_result = MergeRequestOutputSchema(
                success=True,
                status="Updated",
                srpm_path=Path("/git-repos/package/package-2-1.src.rpm"),
                files_to_git_add=None,
                error=None,
            )
            workflow.set_start("run_build_agent")
            workflow.steps["stage_changes"].handler = stop
            workflow.steps["comment_in_mr"].handler = stop
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_build)

    state = await merge_request_agent.run_workflow(
        merge_request_url="https://gitlab.invalid/pkg/-/merge_requests/1",
        dry_run=True,
    )

    assert len(completed) == 1, "Build validation did not reach its success route"

    # The build is pushed to a dedicated scratch branch, never the live MR branch.
    assert len(push_calls) == 1
    _local, pushed_fork, pushed_branch, _tools = push_calls[0]
    assert pushed_fork == fork_url
    assert pushed_branch == f"{update_branch}-ymir-konflux-build"
    assert pushed_branch != update_branch

    # The working tree is restored (soft reset to the captured base) after pushing.
    soft_resets = [c for c in check_calls if c[:3] == ["git", "reset", "--soft"]]
    assert soft_resets == [["git", "reset", "--soft", base_sha]]

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
    }
    assert state.mr_update_result.success is True


@pytest.mark.asyncio
async def test_build_failure_retries_via_registered_step(monkeypatch):
    """A non-timeout, non-infra build failure with attempts left must route back
    to the registered retry step. The handler previously returned
    "prepare_dist_git_from_merge_request", which is not a registered step and
    would abort the workflow instead of retrying."""
    package = "package"

    async def _mock_call_tool(*_args, **_kwargs):
        # No artifacts_urls -> run_build skips diagnosis and reports a plain
        # (retryable) failure.
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"success": False, "error_message": "boom"}))]
        )

    _mock_session = flexmock()
    _mock_session.should_receive("call_tool").replace_with(_mock_call_tool)
    tool = _konflux_build_tool(_mock_session)

    @contextlib.asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield [tool]

    flexmock(merge_request_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(merge_request_agent).should_receive("create_merge_request_agent").and_return(flexmock())
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")
    monkeypatch.setenv("BUILD_BACKEND", "konflux")

    async def _noop(*_args, **_kwargs):
        return None

    async def _mock_commit_changes(*_args, **_kwargs):
        return "abc123def456abc123def456abc123def456abcd"  # pragma: allowlist secret

    monkeypatch.setattr(merge_request_agent.tasks, "stage_changes", _noop)
    monkeypatch.setattr(merge_request_agent.tasks, "commit_changes", _mock_commit_changes)
    monkeypatch.setattr(merge_request_agent.tasks, "push_changes", _noop)

    async def _mock_check_subprocess(cmd, *_args, **_kwargs):
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return ("0000000000000000000000000000000000000000\n", "")  # pragma: allowlist secret
        return ("", "")

    monkeypatch.setattr(merge_request_agent, "check_subprocess", _mock_check_subprocess)

    run_workflow = Workflow.run
    retried = []

    def stop_on_retry(state):
        retried.append(state)
        return Workflow.END

    def start_at_build(workflow, state, options=None):
        if workflow.name == "MergeRequestWorkflow":
            state.local_clone = Path("/git-repos/package")
            state.fork_url = "https://fork.invalid/package.git"
            state.update_branch = "mr-source-branch"
            state.jira_issue = "RHEL-12345"
            state.package = package
            state.dist_git_branch = "c10s"
            state.all_files_git_to_add = set()
            state.mr_update_result = MergeRequestOutputSchema(
                success=True,
                status="Updated",
                srpm_path=Path("/git-repos/package/package-2-1.src.rpm"),
                files_to_git_add=None,
                error=None,
            )
            workflow.set_start("run_build_agent")
            # The retry target: reaching this proves the returned step name is valid.
            workflow.steps["prepare_dist_git_from_mr"].handler = stop_on_retry
            workflow.steps["comment_in_mr"].handler = stop_on_retry
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_build)

    state = await merge_request_agent.run_workflow(
        merge_request_url="https://gitlab.invalid/pkg/-/merge_requests/1",
        dry_run=True,
        max_build_attempts=3,
    )

    assert len(retried) == 1, "Build failure did not route to the registered retry step"
    # One attempt was consumed and the retry carries the build error forward.
    assert state.attempts_remaining == 2
    assert state.build_error == "boom"
