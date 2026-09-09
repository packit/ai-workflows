"""Shared tool execution preserves raw results unless a schema is requested."""

import asyncio

import pytest
from beeai_framework.tools import ToolError
from beeai_framework.tools.types import JSONToolOutput, StringToolOutput
from flexmock import flexmock
from mcp.types import TextContent
from pydantic import BaseModel, ValidationError

from ymir.common.utils import run_tool


class ExampleResult(BaseModel):
    value: int


@pytest.mark.parametrize(
    "output,expected",
    [
        (StringToolOutput("ordinary text"), "ordinary text"),
        (StringToolOutput('{"value": 42}'), '{"value": 42}'),
        (StringToolOutput("42"), "42"),
        (JSONToolOutput({"value": 42}), {"value": 42}),
        (JSONToolOutput({"result": {"value": 42}}), {"value": 42}),
        (JSONToolOutput(TextContent(type="text", text="ordinary text")), "ordinary text"),
        (
            JSONToolOutput([TextContent(type="text", text="first"), {"result": {"value": 42}}]),
            ["first", {"value": 42}],
        ),
    ],
)
@pytest.mark.asyncio
async def test_raw_results_are_unchanged(output, expected):
    middleware_awaited = False

    async def _mock_middleware(*_args, **_kwargs):
        nonlocal middleware_awaited
        middleware_awaited = True
        return output

    _mock_tool = flexmock(name="example")
    _mock_tool.should_receive("run").with_args(input={"query": "hello"}).and_return(
        flexmock(middleware=_mock_middleware)
    ).once()

    assert await run_tool(_mock_tool, query="hello") == expected
    assert middleware_awaited


@pytest.mark.parametrize(
    "output",
    [
        StringToolOutput('{"value": 42}'),
        JSONToolOutput({"value": 42}),
        JSONToolOutput({"result": {"value": 42}}),
        JSONToolOutput(TextContent(type="text", text='{"value": 42}')),
    ],
)
@pytest.mark.parametrize("by_name", [False, True])
@pytest.mark.asyncio
async def test_expected_output_decodes_and_validates(output, by_name):
    middleware_awaited = False

    async def _mock_middleware(*_args, **_kwargs):
        nonlocal middleware_awaited
        middleware_awaited = True
        return output

    _mock_tool = flexmock(name="example")
    _mock_tool.should_receive("run").with_args(input={"query": "hello"}).and_return(
        flexmock(middleware=_mock_middleware)
    ).once()

    result = await run_tool(
        _mock_tool.name if by_name else _mock_tool,
        available_tools=[_mock_tool],
        expected_output=ExampleResult,
        query="hello",
    )

    assert isinstance(result, ExampleResult)
    assert result.value == 42
    assert middleware_awaited
    # The output schema is a helper option, never a tool input.


@pytest.mark.parametrize(
    "output",
    [
        StringToolOutput("not json"),
        JSONToolOutput({}),
        JSONToolOutput({"value": "not an integer"}),
        JSONToolOutput([]),
    ],
)
@pytest.mark.asyncio
async def test_invalid_expected_output_raises_without_retry(output):
    middleware_awaited = False

    async def _mock_middleware(*_args, **_kwargs):
        nonlocal middleware_awaited
        middleware_awaited = True
        return output

    _mock_tool = flexmock(name="example")
    _mock_tool.should_receive("run").and_return(flexmock(middleware=_mock_middleware)).once()

    with pytest.raises(ValidationError):
        await run_tool(_mock_tool, expected_output=ExampleResult)
    assert middleware_awaited


@pytest.mark.parametrize("available", ["none", "empty", "unrelated"])
@pytest.mark.asyncio
async def test_missing_tool_raises_tool_error(available):
    _mock_tool = flexmock(name="example")
    _mock_tool.should_receive("run").never()

    tools = {"none": None, "empty": [], "unrelated": [_mock_tool]}[available]

    with pytest.raises(ToolError, match="missing_tool"):
        await run_tool("missing_tool", available_tools=tools)


@pytest.mark.parametrize(
    "error", [ToolError("tool failed"), RuntimeError("unexpected"), asyncio.CancelledError()]
)
@pytest.mark.asyncio
async def test_execution_errors_propagate_without_retry(error):
    middleware_awaited = False

    async def _mock_middleware(*_args, **_kwargs):
        nonlocal middleware_awaited
        middleware_awaited = True
        raise error

    _mock_tool = flexmock(name="example")
    _mock_tool.should_receive("run").and_return(flexmock(middleware=_mock_middleware)).once()

    with pytest.raises(type(error)) as exc:
        await run_tool(_mock_tool, expected_output=ExampleResult)

    assert exc.value is error
