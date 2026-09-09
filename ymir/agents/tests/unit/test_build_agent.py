"""Build submission is deterministic; only failed-build diagnosis needs an LLM."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from beeai_framework.emitter import Emitter
from beeai_framework.errors import AbortError, FrameworkError
from beeai_framework.tools import ToolError
from beeai_framework.tools.mcp import MCPTool
from flexmock import flexmock
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


async def _run(build_input, tools):
    return await build_agent.run_build(
        build_input=build_input,
        available_tools=tools,
        local_tool_options={"working_directory": Path("/git-repos/package")},
    )


@pytest.mark.asyncio
async def test_success_submits_once_without_llm(build_input, monkeypatch):
    async def _mock_submit(*_args, **_kwargs):
        return BuildResult(success=True, artifacts_urls=["https://copr/log.gz"])

    _mock_tool = SimpleNamespace(name="build_package")
    flexmock(build_agent).should_receive("run_tool").with_args(
        "build_package",
        available_tools=[_mock_tool],
        expected_output=BuildResult,
        srpm_path=str(build_input.srpm_path),
        dist_git_branch="c10s",
        jira_issue="RHEL-123",
    ).replace_with(_mock_submit).once()
    flexmock(build_agent).should_receive("create_build_failure_agent").never()
    # Successful builds must not even require model configuration.
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    result = await _run(build_input, [_mock_tool])

    assert result.model_dump() == {
        "success": True,
        "error": None,
        "is_timeout": False,
        "is_infra_error": False,
    }


@pytest.mark.asyncio
async def test_timeout_preserved_without_llm_even_with_logs(build_input, monkeypatch):
    async def _mock_submit(*_args, **_kwargs):
        return BuildResult(
            success=False,
            is_timeout=True,
            error_message="Reached timeout for build 123",
            artifacts_urls=["https://copr/123/builder-live.log.gz"],
        )

    _mock_tool = SimpleNamespace(name="build_package")
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    flexmock(build_agent).should_receive("run_tool").replace_with(_mock_submit)
    flexmock(build_agent).should_receive("create_build_failure_agent").never()

    result = await _run(build_input, [_mock_tool])

    assert not result.success
    assert result.is_timeout
    assert not result.is_infra_error
    assert result.error == "Reached timeout for build 123"


@pytest.mark.asyncio
async def test_tool_error_is_infrastructure_failure_without_resubmitting(build_input, monkeypatch):
    async def _mock_submit(*_args, **_kwargs):
        raise ToolError("Failed to submit Copr build: HTTP 503")

    _mock_tool = SimpleNamespace(name="build_package")
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    flexmock(build_agent).should_receive("run_tool").replace_with(_mock_submit).once()
    flexmock(build_agent).should_receive("create_build_failure_agent").never()

    result = await _run(build_input, [_mock_tool])

    assert not result.success
    assert result.is_infra_error
    assert not result.is_timeout
    assert "HTTP 503" in result.error


@pytest.mark.parametrize(
    "tool_names", [[], ["download_artifacts", "extract_log_snippets"]], ids=["empty", "unrelated"]
)
@pytest.mark.asyncio
async def test_missing_build_tool_is_infrastructure_failure(build_input, monkeypatch, tool_names):
    _mock_session = flexmock()
    _mock_session.should_receive("call_tool").never()
    _mock_tools = [MCPTool(_mock_session, MCPToolInfo(name=name, inputSchema={})) for name in tool_names]
    flexmock(build_agent).should_receive("create_build_failure_agent").never()
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    # Exercise the real lookup rather than mocking run_tool.
    output = await _run(build_input, _mock_tools)

    assert not output.success
    assert output.is_infra_error
    assert not output.is_timeout
    assert "build_package" in output.error


@pytest.mark.asyncio
async def test_unexpected_submission_error_propagates(build_input, monkeypatch):
    _mock_error = RuntimeError("Unexpected submission error")

    async def _mock_submit(*_args, **_kwargs):
        raise _mock_error

    _mock_tool = SimpleNamespace(name="build_package")
    flexmock(build_agent).should_receive("run_tool").replace_with(_mock_submit).once()
    flexmock(build_agent).should_receive("create_build_failure_agent").never()
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    with pytest.raises(FrameworkError) as exc:
        await _run(build_input, [_mock_tool])

    assert exc.value.__cause__ == _mock_error


@pytest.mark.parametrize("artifacts", [None, [], ["https://copr/package.rpm"]])
@pytest.mark.asyncio
async def test_failure_without_logs_returns_original_error(build_input, monkeypatch, artifacts):
    async def _mock_submit(*_args, **_kwargs):
        return BuildResult(
            success=False,
            error_message="Build 123 finished with state: failed",
            artifacts_urls=artifacts,
        )

    _mock_tool = SimpleNamespace(name="build_package")
    flexmock(build_agent).should_receive("run_tool").replace_with(_mock_submit).once()
    flexmock(build_agent).should_receive("create_build_failure_agent").never()
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    result = await _run(build_input, [_mock_tool])

    assert not result.success
    assert not result.is_infra_error
    assert result.error == "Build 123 finished with state: failed"


@pytest.mark.asyncio
async def test_failure_analysis_receives_existing_result_and_cannot_resubmit(build_input, monkeypatch):
    log_url = "https://copr/123/builder-live.log.gz?download=1"

    async def _mock_submit(*_args, **_kwargs):
        return BuildResult(
            success=False,
            error_message="Build 123 finished with state: failed",
            artifacts_urls=[log_url],
        )

    analyst_run_args = []

    async def _mock_analyst(*_args, **_kwargs):
        analyst_run_args.append((_args, _kwargs))
        return SimpleNamespace(
            last_message=SimpleNamespace(text='{"error": "Missing prerequisite function foo"}')
        )

    def _mock_factory(*_args, **_kwargs):
        _mock_run = flexmock()
        _mock_run.should_receive("run").replace_with(_mock_analyst).once()
        return _mock_run

    _mock_tool = SimpleNamespace(name="build_package")
    flexmock(build_agent).should_receive("run_tool").replace_with(_mock_submit).once()
    flexmock(build_agent).should_receive("create_build_failure_agent").replace_with(_mock_factory).once()
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    result = await _run(build_input, [_mock_tool])

    assert not result.success
    assert not result.is_timeout
    assert not result.is_infra_error
    assert result.error == "Missing prerequisite function foo"
    args, kwargs = analyst_run_args[0]
    assert "Build 123 finished with state: failed" in args[0]
    assert log_url in args[0]
    assert str(build_input.srpm_path) in args[0]
    assert kwargs["expected_output"] is BuildFailureAnalysisOutput
    assert set(BuildFailureAnalysisOutput.model_fields) == {"error"}


@pytest.mark.parametrize("analysis_error", [True, False])
@pytest.mark.asyncio
async def test_diagnosis_failure_retains_build_failure(build_input, monkeypatch, analysis_error):
    async def _mock_submit(*_args, **_kwargs):
        return BuildResult(
            success=False,
            error_message="Build 123 failed",
            artifacts_urls=["https://copr/123/root.log.gz"],
        )

    async def _mock_analyst(*_args, **_kwargs):
        if analysis_error:
            raise RuntimeError("Model unavailable")
        return SimpleNamespace(last_message=SimpleNamespace(text="not json"))

    def _mock_factory(*_args, **_kwargs):
        return SimpleNamespace(run=_mock_analyst)

    _mock_tool = SimpleNamespace(name="build_package")
    flexmock(build_agent).should_receive("run_tool").replace_with(_mock_submit).once()
    flexmock(build_agent).should_receive("create_build_failure_agent").replace_with(_mock_factory)
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    result = await _run(build_input, [_mock_tool])

    assert not result.success
    assert not result.is_infra_error
    assert "Build 123 failed" in result.error


@pytest.mark.parametrize("raw", ["not json", {}, {"success": "unknown"}, []])
@pytest.mark.asyncio
async def test_malformed_tool_result_is_not_reported_as_success(build_input, monkeypatch, raw):

    async def _mock_call_tool(*_args, **_kwargs):
        return CallToolResult(
            content=[TextContent(type="text", text=raw if isinstance(raw, str) else json.dumps(raw))],
        )

    _mock_session = flexmock()
    _mock_session.should_receive("call_tool").replace_with(_mock_call_tool).once()
    _mock_tool = MCPTool(
        _mock_session, MCPToolInfo(name="build_package", inputSchema=BuildInputSchema.model_json_schema())
    )

    flexmock(build_agent).should_receive("create_build_failure_agent").never()
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    with pytest.raises(FrameworkError) as exc:
        await _run(build_input, [_mock_tool])

    assert isinstance(exc.value.__cause__, ValidationError)


@pytest.mark.parametrize("during_analysis", [False, True])
@pytest.mark.parametrize("cancellation", [asyncio.CancelledError, AbortError])
@pytest.mark.asyncio
async def test_cancellation_propagates(build_input, monkeypatch, during_analysis, cancellation):
    async def _mock_submit(*_args, **_kwargs):
        if during_analysis:
            return BuildResult(
                success=False,
                artifacts_urls=["https://copr/123/root.log.gz"],
            )
        raise cancellation

    async def _mock_analyst(*_args, **_kwargs):
        if during_analysis:
            raise cancellation
        return SimpleNamespace(
            last_message=SimpleNamespace(text='{"error": "Missing prerequisite function foo"}'),
        )

    def _mock_factory(*_args, **_kwargs):
        return SimpleNamespace(run=_mock_analyst)

    _mock_tool = SimpleNamespace(name="build_package")
    flexmock(build_agent).should_receive("run_tool").replace_with(_mock_submit).once()
    flexmock(build_agent).should_receive("create_build_failure_agent").replace_with(_mock_factory)
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    with pytest.raises(asyncio.CancelledError):
        await _run(build_input, [_mock_tool])


@pytest.mark.parametrize("during_analysis", [False, True])
@pytest.mark.asyncio
async def test_cancelling_caller_stops_active_workflow_step(build_input, monkeypatch, during_analysis):
    started = asyncio.Event()
    stopped = asyncio.Event()
    active_step = None

    async def _mock_blocked_step(*_args, **_kwargs):
        nonlocal active_step
        active_step = asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def _mock_submit(*_args, **_kwargs):
        return BuildResult(success=False, artifacts_urls=["https://copr/123/root.log.gz"])

    async def _mock_analyst(*_args, **_kwargs):
        return SimpleNamespace(
            last_message=SimpleNamespace(text='{"error": "Missing prerequisite function foo"}'),
        )

    def _mock_factory(*_args, **_kwargs):
        return SimpleNamespace(run=_mock_blocked_step if during_analysis else _mock_analyst)

    _mock_tool = SimpleNamespace(name="build_package")
    flexmock(build_agent).should_receive("run_tool").replace_with(
        _mock_submit if during_analysis else _mock_blocked_step
    ).once()
    flexmock(build_agent).should_receive("create_build_failure_agent").replace_with(_mock_factory).times(
        during_analysis
    )
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    task = asyncio.create_task(_run(build_input, [_mock_tool]))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        await asyncio.wait_for(stopped.wait(), timeout=2)
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
async def test_actual_gateway_result_decoding(build_input, structured, success, is_timeout, error_message):
    result = {
        "success": success,
        "is_timeout": is_timeout,
        "error_message": error_message,
        "artifacts_urls": [],
    }

    call_tool_args = []

    async def _mock_call_tool(*_args, **_kwargs):
        call_tool_args.append((_args, _kwargs))
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))],
            structuredContent={"result": result} if structured else None,
        )

    _mock_session = flexmock()
    _mock_session.should_receive("call_tool").replace_with(_mock_call_tool).once()
    tool = MCPTool(
        _mock_session,
        MCPToolInfo(name="build_package", inputSchema=BuildInputSchema.model_json_schema()),
    )
    flexmock(build_agent).should_receive("create_build_failure_agent").never()

    output = await _run(build_input, [tool])

    assert output.success == success
    assert output.is_timeout == is_timeout
    assert output.error == error_message
    assert not output.is_infra_error

    _, kwargs = call_tool_args[0]
    assert kwargs["arguments"] == build_input.model_dump(mode="json")


@pytest.mark.asyncio
async def test_actual_gateway_error_is_infrastructure_failure(build_input):
    async def _mock_call_tool(*_args, **_kwargs):
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text="Failed to submit Copr build: HTTP 503")],
        )

    _mock_session = flexmock()
    _mock_session.should_receive("call_tool").replace_with(_mock_call_tool).once()
    _mock_tool = MCPTool(
        _mock_session,
        MCPToolInfo(name="build_package", inputSchema=BuildInputSchema.model_json_schema()),
    )

    flexmock(build_agent).should_receive("create_build_failure_agent").never()

    output = await _run(build_input, [_mock_tool])

    assert not output.success
    assert output.is_infra_error
    assert not output.is_timeout
    assert "HTTP 503" in output.error


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
async def test_build_workflow_routes_real_steps(build_input, build_result, steps):
    async def _mock_call_tool(*_args, **_kwargs):
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(build_result))])

    _mock_session = flexmock()
    _mock_session.should_receive("call_tool").replace_with(_mock_call_tool).once()
    _mock_tool = MCPTool(
        _mock_session, MCPToolInfo(name="build_package", inputSchema=BuildInputSchema.model_json_schema())
    )

    async def _mock_analyst_run(*_args, **_kwargs):
        return SimpleNamespace(last_message=SimpleNamespace(text='{"error": "Diagnosis"}'))

    def _mock_factory(*_args, **_kwargs):
        return SimpleNamespace(run=_mock_analyst_run)

    flexmock(build_agent).should_receive("create_build_failure_agent").replace_with(_mock_factory).times(
        "diagnose_failure" in steps
    )
    visited = []

    def on_step(data, event):
        if event.name == "start" and getattr(event.creator, "name", None) == "BuildWorkflow":
            visited.append(data.step)

    cleanup = Emitter.root().on("*.*", on_step)
    try:
        output = await _run(build_input, [_mock_tool])
    finally:
        cleanup()

    assert visited == steps
    assert output.success == build_result["success"]


@pytest.mark.parametrize("has_extractor", [False, True])
@pytest.mark.parametrize("has_downloader", [False, True])
def test_failure_agent_has_no_build_or_edit_tools(has_extractor, has_downloader):
    names = ["build_package", "unrelated_tool"]
    if has_downloader:
        names.append("download_artifacts")
    if has_extractor:
        names.append("extract_log_snippets")
    tools = [SimpleNamespace(name=name) for name in names]

    factory_call_args = []

    def _mock_factory(*_args, **_kwargs):
        factory_call_args.append((_args, _kwargs))

    flexmock(build_agent).should_receive("ReasoningAgent").replace_with(_mock_factory)
    flexmock(build_agent).should_receive("get_chat_model").replace_with(flexmock())

    build_agent.create_build_failure_agent(tools, {"working_directory": Path("/tmp")})

    _, kwargs = factory_call_args[0]
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
