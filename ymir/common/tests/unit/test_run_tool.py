"""Shared tool execution preserves raw results unless a schema is requested."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from beeai_framework.tools import Tool, ToolError
from beeai_framework.tools.types import JSONToolOutput, StringToolOutput
from mcp.types import TextContent
from pydantic import BaseModel, ValidationError

from ymir.common.utils import run_tool


class ExampleResult(BaseModel):
    value: int


@pytest.fixture
def tool():
    tool = MagicMock(spec=Tool)
    tool.name = "example"
    tool.run.return_value.middleware = AsyncMock()
    return tool


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
async def test_raw_results_are_unchanged(tool, output, expected):
    tool.run.return_value.middleware.return_value = output

    assert await run_tool(tool, query="hello") == expected

    tool.run.assert_called_once_with(input={"query": "hello"})
    tool.run.return_value.middleware.assert_awaited_once()


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
async def test_expected_output_decodes_and_validates(tool, output, by_name):
    tool.run.return_value.middleware.return_value = output

    result = await run_tool(
        tool.name if by_name else tool,
        available_tools=[tool],
        expected_output=ExampleResult,
        query="hello",
    )

    assert isinstance(result, ExampleResult)
    assert result.value == 42
    # The output schema is a helper option, never a tool input.
    tool.run.assert_called_once_with(input={"query": "hello"})
    tool.run.return_value.middleware.assert_awaited_once()


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
async def test_invalid_expected_output_raises_without_retry(tool, output):
    tool.run.return_value.middleware.return_value = output

    with pytest.raises(ValidationError):
        await run_tool(tool, expected_output=ExampleResult)

    tool.run.assert_called_once()
    tool.run.return_value.middleware.assert_awaited_once()


@pytest.mark.parametrize("available", ["none", "empty", "unrelated"])
@pytest.mark.asyncio
async def test_missing_tool_raises_tool_error(tool, available):
    tools = {"none": None, "empty": [], "unrelated": [tool]}[available]

    with pytest.raises(ToolError, match="missing_tool"):
        await run_tool("missing_tool", available_tools=tools)

    tool.run.assert_not_called()


@pytest.mark.parametrize(
    "error", [ToolError("tool failed"), RuntimeError("unexpected"), asyncio.CancelledError()]
)
@pytest.mark.asyncio
async def test_execution_errors_propagate_without_retry(tool, error):
    tool.run.return_value.middleware.side_effect = error

    with pytest.raises(type(error)) as exc:
        await run_tool(tool, expected_output=ExampleResult)

    assert exc.value is error
    tool.run.assert_called_once()
    tool.run.return_value.middleware.assert_awaited_once()
