"""Build submission is deterministic; only failed-build diagnosis needs an LLM."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from beeai_framework.emitter import Emitter
from beeai_framework.errors import AbortError, FrameworkError
from beeai_framework.tools import ToolError
from beeai_framework.tools.mcp import MCPTool
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPToolInfo
from pydantic import ValidationError

from ymir.agents import build_agent
from ymir.common.models import BuildFailureAnalysisOutput, BuildInputSchema, BuildResult


@pytest.fixture
def build_input():
    return BuildInputSchema(
        srpm_path=Path("/git-repos/package/package-1-1.src.rpm"),
        dist_git_branch="c10s",
        jira_issue="RHEL-123",
    )


@pytest.fixture
def build_mocks(monkeypatch):
    tool = SimpleNamespace(name="build_package")
    submit = AsyncMock()
    analyst = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                last_message=SimpleNamespace(text='{"error": "Missing prerequisite function foo"}'),
            ),
        ),
    )
    factory = MagicMock(return_value=analyst)
    monkeypatch.setattr(build_agent, "run_tool", submit)
    monkeypatch.setattr(build_agent, "create_build_failure_agent", factory)
    # Successful builds must not even require model configuration.
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    return SimpleNamespace(tool=tool, submit=submit, analyst=analyst, factory=factory)


async def _run(build_input, tools):
    return await build_agent.run_build(
        build_input=build_input,
        available_tools=tools,
        local_tool_options={"working_directory": Path("/git-repos/package")},
    )


@pytest.mark.asyncio
async def test_success_submits_once_without_llm(build_input, build_mocks):
    build_mocks.submit.return_value = BuildResult(success=True, artifacts_urls=["https://copr/log.gz"])

    result = await _run(build_input, [build_mocks.tool])

    assert result.model_dump() == {
        "success": True,
        "error": None,
        "is_timeout": False,
        "is_infra_error": False,
    }
    build_mocks.submit.assert_awaited_once_with(
        "build_package",
        available_tools=[build_mocks.tool],
        expected_output=BuildResult,
        srpm_path=str(build_input.srpm_path),
        dist_git_branch="c10s",
        jira_issue="RHEL-123",
    )
    build_mocks.factory.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_preserved_without_llm_even_with_logs(build_input, build_mocks):
    build_mocks.submit.return_value = BuildResult(
        success=False,
        is_timeout=True,
        error_message="Reached timeout for build 123",
        artifacts_urls=["https://copr/123/builder-live.log.gz"],
    )

    result = await _run(build_input, [build_mocks.tool])

    assert not result.success
    assert result.is_timeout
    assert not result.is_infra_error
    assert result.error == "Reached timeout for build 123"
    build_mocks.factory.assert_not_called()


@pytest.mark.asyncio
async def test_tool_error_is_infrastructure_failure_without_resubmitting(build_input, build_mocks):
    build_mocks.submit.side_effect = ToolError("Failed to submit Copr build: HTTP 503")

    result = await _run(build_input, [build_mocks.tool])

    assert not result.success
    assert result.is_infra_error
    assert not result.is_timeout
    assert "HTTP 503" in result.error
    build_mocks.submit.assert_awaited_once()
    build_mocks.factory.assert_not_called()


@pytest.mark.parametrize(
    "tool_names", [[], ["download_artifacts", "extract_log_snippets"]], ids=["empty", "unrelated"]
)
@pytest.mark.asyncio
async def test_missing_build_tool_is_infrastructure_failure(build_input, monkeypatch, tool_names):
    session = AsyncMock()
    tools = [MCPTool(session, MCPToolInfo(name=name, inputSchema={})) for name in tool_names]
    factory = MagicMock()
    monkeypatch.setattr(build_agent, "create_build_failure_agent", factory)
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    # Exercise the real lookup rather than mocking run_tool.
    output = await _run(build_input, tools)

    assert not output.success
    assert output.is_infra_error
    assert not output.is_timeout
    assert "build_package" in output.error
    session.call_tool.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_submission_error_propagates(build_input, build_mocks):
    error = RuntimeError("Unexpected submission error")
    build_mocks.submit.side_effect = error

    with pytest.raises(FrameworkError) as exc:
        await _run(build_input, [build_mocks.tool])

    assert exc.value.__cause__ is error
    build_mocks.submit.assert_awaited_once()
    build_mocks.factory.assert_not_called()


@pytest.mark.parametrize("artifacts", [None, [], ["https://copr/package.rpm"]])
@pytest.mark.asyncio
async def test_failure_without_logs_returns_original_error(build_input, build_mocks, artifacts):
    build_mocks.submit.return_value = BuildResult(
        success=False,
        error_message="Build 123 finished with state: failed",
        artifacts_urls=artifacts,
    )

    result = await _run(build_input, [build_mocks.tool])

    assert not result.success
    assert not result.is_infra_error
    assert result.error == "Build 123 finished with state: failed"
    build_mocks.factory.assert_not_called()


@pytest.mark.asyncio
async def test_failure_analysis_receives_existing_result_and_cannot_resubmit(build_input, build_mocks):
    log_url = "https://copr/123/builder-live.log.gz?download=1"
    build_mocks.submit.return_value = BuildResult(
        success=False,
        error_message="Build 123 finished with state: failed",
        artifacts_urls=[log_url],
    )

    result = await _run(build_input, [build_mocks.tool])

    assert not result.success
    assert not result.is_timeout
    assert not result.is_infra_error
    assert result.error == "Missing prerequisite function foo"
    build_mocks.submit.assert_awaited_once()
    build_mocks.factory.assert_called_once()
    build_mocks.analyst.run.assert_awaited_once()
    args, kwargs = build_mocks.analyst.run.await_args
    assert "Build 123 finished with state: failed" in args[0]
    assert log_url in args[0]
    assert str(build_input.srpm_path) in args[0]
    assert kwargs["expected_output"] is BuildFailureAnalysisOutput
    assert set(BuildFailureAnalysisOutput.model_fields) == {"error"}


@pytest.mark.parametrize("analysis_error", [RuntimeError("Model unavailable"), None])
@pytest.mark.asyncio
async def test_diagnosis_failure_retains_build_failure(build_input, build_mocks, analysis_error):
    build_mocks.submit.return_value = BuildResult(
        success=False,
        error_message="Build 123 failed",
        artifacts_urls=["https://copr/123/root.log.gz"],
    )
    if analysis_error:
        build_mocks.analyst.run.side_effect = analysis_error
    else:
        build_mocks.analyst.run.return_value.last_message.text = "not json"

    result = await _run(build_input, [build_mocks.tool])

    assert not result.success
    assert not result.is_infra_error
    assert "Build 123 failed" in result.error
    build_mocks.submit.assert_awaited_once()


@pytest.mark.parametrize("raw", ["not json", {}, {"success": "unknown"}, []])
@pytest.mark.asyncio
async def test_malformed_tool_result_is_not_reported_as_success(build_input, monkeypatch, raw):
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=raw if isinstance(raw, str) else json.dumps(raw))],
    )
    tool = MCPTool(
        session, MCPToolInfo(name="build_package", inputSchema=BuildInputSchema.model_json_schema())
    )
    factory = MagicMock()
    monkeypatch.setattr(build_agent, "create_build_failure_agent", factory)

    with pytest.raises(FrameworkError) as exc:
        await _run(build_input, [tool])

    assert isinstance(exc.value.__cause__, ValidationError)
    factory.assert_not_called()
    session.call_tool.assert_awaited_once()


@pytest.mark.parametrize("during_analysis", [False, True])
@pytest.mark.parametrize("cancellation", [asyncio.CancelledError, AbortError])
@pytest.mark.asyncio
async def test_cancellation_propagates(build_input, build_mocks, during_analysis, cancellation):
    if during_analysis:
        build_mocks.submit.return_value = BuildResult(
            success=False,
            artifacts_urls=["https://copr/123/root.log.gz"],
        )
        build_mocks.analyst.run.side_effect = cancellation
    else:
        build_mocks.submit.side_effect = cancellation

    with pytest.raises(asyncio.CancelledError):
        await _run(build_input, [build_mocks.tool])

    build_mocks.submit.assert_awaited_once()


@pytest.mark.parametrize("during_analysis", [False, True])
@pytest.mark.asyncio
async def test_cancelling_caller_stops_active_workflow_step(build_input, build_mocks, during_analysis):
    started = asyncio.Event()
    stopped = asyncio.Event()
    active_step = None

    async def blocked_step(*args, **kwargs):
        nonlocal active_step
        active_step = asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    if during_analysis:
        build_mocks.submit.return_value = BuildResult(
            success=False, artifacts_urls=["https://copr/123/root.log.gz"]
        )
        build_mocks.analyst.run.side_effect = blocked_step
    else:
        build_mocks.submit.side_effect = blocked_step

    task = asyncio.create_task(_run(build_input, [build_mocks.tool]))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        await asyncio.wait_for(stopped.wait(), timeout=2)
        build_mocks.submit.assert_awaited_once()
        assert build_mocks.factory.call_count == during_analysis
    finally:
        task.cancel()
        if active_step is not None:
            active_step.cancel()
            await asyncio.gather(active_step, return_exceptions=True)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize(
    "success,is_timeout,error_message",
    [(True, False, None), (False, True, "Build timed out"), (False, False, "Build failed")],
)
@pytest.mark.asyncio
async def test_actual_gateway_result_decoding(
    build_input, monkeypatch, structured, success, is_timeout, error_message
):
    result = {
        "success": success,
        "is_timeout": is_timeout,
        "error_message": error_message,
        "artifacts_urls": [],
    }
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result))],
        structuredContent={"result": result} if structured else None,
    )
    tool = MCPTool(
        session,
        MCPToolInfo(name="build_package", inputSchema=BuildInputSchema.model_json_schema()),
    )
    factory = MagicMock()
    monkeypatch.setattr(build_agent, "create_build_failure_agent", factory)

    output = await _run(build_input, [tool])

    assert output.success == success
    assert output.is_timeout == is_timeout
    assert output.error == error_message
    assert not output.is_infra_error
    session.call_tool.assert_awaited_once()
    assert session.call_tool.await_args.kwargs["arguments"] == build_input.model_dump(mode="json")
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_actual_gateway_error_is_infrastructure_failure(build_input, monkeypatch):
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        isError=True,
        content=[TextContent(type="text", text="Failed to submit Copr build: HTTP 503")],
    )
    tool = MCPTool(
        session,
        MCPToolInfo(name="build_package", inputSchema=BuildInputSchema.model_json_schema()),
    )
    factory = MagicMock()
    monkeypatch.setattr(build_agent, "create_build_failure_agent", factory)

    output = await _run(build_input, [tool])

    assert not output.success
    assert output.is_infra_error
    assert not output.is_timeout
    assert "HTTP 503" in output.error
    session.call_tool.assert_awaited_once()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "build_result,steps",
    [
        ({"success": True}, ["execute_build"]),
        ({"success": False, "is_timeout": True}, ["execute_build"]),
        ({"success": False}, ["execute_build"]),
        (
            {"success": False, "artifacts_urls": ["https://copr/builder-live.log.gz"]},
            ["execute_build", "diagnose_failure"],
        ),
    ],
    ids=["success", "timeout", "no-logs", "diagnosis"],
)
@pytest.mark.asyncio
async def test_build_workflow_routes_real_steps(build_input, monkeypatch, build_result, steps):
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(build_result))]
    )
    tool = MCPTool(
        session, MCPToolInfo(name="build_package", inputSchema=BuildInputSchema.model_json_schema())
    )
    analyst = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(last_message=SimpleNamespace(text='{"error": "Diagnosis"}'))
        )
    )
    factory = MagicMock(return_value=analyst)
    monkeypatch.setattr(build_agent, "create_build_failure_agent", factory)
    visited = []

    def on_step(data, event):
        if event.name == "start" and getattr(event.creator, "name", None) == "BuildWorkflow":
            visited.append(data.step)

    cleanup = Emitter.root().on("*.*", on_step)
    try:
        output = await _run(build_input, [tool])
    finally:
        cleanup()

    assert visited == steps
    assert output.success == build_result["success"]
    session.call_tool.assert_awaited_once()
    assert factory.call_count == ("diagnose_failure" in steps)


@pytest.mark.parametrize("has_extractor", [False, True])
@pytest.mark.parametrize("has_downloader", [False, True])
def test_failure_agent_has_no_build_or_edit_tools(monkeypatch, has_extractor, has_downloader):
    names = ["build_package", "unrelated_tool"]
    if has_downloader:
        names.append("download_artifacts")
    if has_extractor:
        names.append("extract_log_snippets")
    tools = [SimpleNamespace(name=name) for name in names]
    factory = MagicMock()
    monkeypatch.setattr(build_agent, "ReasoningAgent", factory)
    monkeypatch.setattr(build_agent, "get_chat_model", MagicMock())

    build_agent.create_build_failure_agent(tools, {"working_directory": Path("/tmp")})

    kwargs = factory.call_args.kwargs
    available = {t.name for t in kwargs["tools"]}
    has_gateway_log_tools = has_extractor and has_downloader
    assert ("download_artifacts" in available) == has_gateway_log_tools
    assert "build_package" not in available
    assert "unrelated_tool" not in available
    assert not available.intersection({"create", "insert", "str_replace", "insert_after_substring"})
    assert ("extract_log_snippets" in available) == has_gateway_log_tools
    local_tools = {"run_shell_command", "view", "search_text", "get_cwd"}
    assert available.intersection(local_tools) == (set() if has_gateway_log_tools else local_tools)
    extractor_requirements = [
        requirement for requirement in kwargs["requirements"] if requirement.source == "extract_log_snippets"
    ]
    assert len(extractor_requirements) == int(has_gateway_log_tools)
    if has_gateway_log_tools:
        assert extractor_requirements[0]._after == {"download_artifacts"}
        assert "accessible through `extract_log_snippets`" in kwargs["instructions"]
    else:
        assert "extract_log_snippets" not in kwargs["instructions"]
        assert "download_artifacts" not in kwargs["instructions"]
        assert "local sandbox" in kwargs["instructions"]
        assert "artifacts_urls" in kwargs["instructions"]
    assert "Do not submit or retry a build" in kwargs["instructions"]
