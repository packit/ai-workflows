"""Consolidation can validate builds even when commits have no Jira footers."""

import contextlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from beeai_framework.tools.mcp import MCPTool
from beeai_framework.workflows import Workflow
from flexmock import flexmock
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPToolInfo

from ymir.agents import mr_consolidation_agent
from ymir.common.models import MRConsolidationOutputSchema


@pytest.mark.parametrize(
    "package,branch",
    [
        ("compat-libstdc++-33", "c10s"),
        ("package", "rhel/10.1"),
        ("a" * 81, "c10s"),
        ("a" * 82, "c10s"),
    ],
)
def test_fallback_build_project_is_valid_and_stable(package, branch):
    project = mr_consolidation_agent._build_project_name(package, branch)

    assert re.fullmatch(r"[A-Za-z0-9_.-]+", project)
    assert len(project) <= 100
    assert project == mr_consolidation_agent._build_project_name(package, branch)
    if package == "a" * 81:
        assert project == f"consolidation-{package}-{branch}"


@pytest.mark.parametrize(
    "first,second",
    [
        (("compat-libstdc++-33", "c10s"), ("compat-libstdc--33", "c10s")),
        (("a" * 100 + "b", "c10s"), ("a" * 100 + "c", "c10s")),
        (("a" * 100, "c9s"), ("a" * 100, "c10s")),
    ],
)
def test_fallback_build_projects_remain_distinct(first, second):
    assert mr_consolidation_agent._build_project_name(*first) != mr_consolidation_agent._build_project_name(
        *second
    )


@pytest.mark.parametrize("jira_issue", [None, "RHEL-12345"])
@pytest.mark.parametrize("release_strategy", ["per_commit", "merged"])
@pytest.mark.parametrize("retry_first", [False, True])
@pytest.mark.parametrize("package", ["package", "compat-libstdc++-33", "package" * 30])
@pytest.mark.asyncio
async def test_build_project_identifier_preserves_jira_metadata(
    monkeypatch, jira_issue, release_strategy, retry_first, package
):
    results = [{"success": True}]
    if retry_first:
        results.insert(0, {"success": False, "error_message": "Build failed"})
    call_tool_results = iter(
        [CallToolResult(content=[TextContent(type="text", text=json.dumps(result))]) for result in results]
    )

    call_tool_args = []

    async def _mock_call_tool(*_args, **_kwargs):
        call_tool_args.append((_args, _kwargs))
        return next(call_tool_results)

    _mock_session = flexmock()
    _mock_session.should_receive("call_tool").replace_with(_mock_call_tool)
    # The real build_package contract requires a string project identifier,
    # even though the workflow's Jira metadata is optional.
    tool = MCPTool(
        _mock_session,
        MCPToolInfo(
            name="build_package",
            inputSchema={
                "type": "object",
                "properties": {
                    "srpm_path": {"type": "string"},
                    "dist_git_branch": {"type": "string"},
                    "jira_issue": {"type": "string"},
                },
                "required": ["srpm_path", "dist_git_branch", "jira_issue"],
            },
        ),
    )

    @contextlib.asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield [tool]

    flexmock(mr_consolidation_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(mr_consolidation_agent).should_receive("create_log_agent").and_return(flexmock())
    flexmock(mr_consolidation_agent).should_receive("get_mock_local_tool_env").and_return(None)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    run_workflow = Workflow.run
    completed = []

    def stop_after_build(state):
        completed.append(state)
        return Workflow.END

    def start_at_build(workflow, state, options=None):
        if workflow.name == "MRConsolidationWorkflow":
            # Start after SRPM preparation and stop before publication. Run the
            # real consolidation build step, nested BuildWorkflow and MCPTool.
            state.jira_issue = jira_issue
            state.jira_issues_collected = [jira_issue] if jira_issue else []
            state.consolidation_result = MRConsolidationOutputSchema(
                success=True,
                status="Prepared SRPM",
                srpm_path=Path("/git-repos/package/package-1-1.src.rpm"),
            )
            workflow.set_start("run_build_agent")
            retry_step = "per_commit_flow" if release_strategy == "per_commit" else "run_consolidation_agent"
            workflow.steps[retry_step].handler = lambda _: "run_build_agent"
            workflow.steps["push_and_open_mr"].handler = stop_after_build
            workflow.steps["update_release"].handler = stop_after_build
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_build)

    state = await mr_consolidation_agent.run_workflow(
        package=package,
        dist_git_branch="c10s",
        release_strategy=release_strategy,
        dry_run=True,
        consolidation_agent_factory=lambda *_args, **_kwargs: SimpleNamespace(),
    )

    assert len(completed) == 1, "Build validation did not reach its success route"
    assert len(call_tool_args) == len(results)

    _, first_kwargs = call_tool_args[0]
    project = first_kwargs["arguments"]["jira_issue"]
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", project)
    assert len(project) <= 100
    if jira_issue:
        assert project == jira_issue
    elif package == "package":
        assert project == "consolidation-package-c10s"
    else:
        assert project.startswith("consolidation-")
    for _, kwargs in call_tool_args:
        assert kwargs["arguments"] == {
            "srpm_path": "/git-repos/package/package-1-1.src.rpm",
            "dist_git_branch": "c10s",
            "jira_issue": project,
        }
    assert state.jira_issue == jira_issue
    assert state.jira_issues_collected == ([jira_issue] if jira_issue else [])
    assert state.attempts_remaining == 3 - int(retry_first)
