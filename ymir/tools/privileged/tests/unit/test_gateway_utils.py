"""Unit tests for ymir.tools.gateway_utils.get_log_detective_mcp."""

import pytest
from beeai_framework.tools.mcp import MCPTool
from flexmock import flexmock
from mcp import StdioServerParameters

import ymir.tools.gateway_utils as gateway_utils_module
from ymir.tools.gateway_utils import get_log_detective_mcp


def _create_async_return(value):
    """Wrap a value in a coroutine so it can be awaited."""

    async def async_return(*args, **kwargs):
        return value

    return async_return()


class TestGetLogDetectiveMcp:
    @pytest.mark.asyncio
    async def test_returns_tools_from_mcp_server(self):
        expected_tools = [flexmock(name="extract_log_snippets")]

        mock_client = flexmock()
        flexmock(gateway_utils_module).should_receive("stdio_client").once().and_return(mock_client)
        flexmock(MCPTool).should_receive("from_client").with_args(mock_client).once().and_return(
            _create_async_return(expected_tools)
        )

        result = await get_log_detective_mcp()

        assert result == expected_tools

    @pytest.mark.asyncio
    async def test_passes_logdetective_mcp_command(self):
        mock_client = flexmock()

        def verify_params(params):
            assert isinstance(params, StdioServerParameters)
            assert params.command == "logdetective-mcp"
            return mock_client

        flexmock(gateway_utils_module).should_receive("stdio_client").replace_with(verify_params)
        flexmock(MCPTool).should_receive("from_client").with_args(mock_client).once().and_return(
            _create_async_return([])
        )

        await get_log_detective_mcp()

    @pytest.mark.asyncio
    async def test_propagates_runtime_error(self):
        mock_client = flexmock()
        flexmock(gateway_utils_module).should_receive("stdio_client").once().and_return(mock_client)
        flexmock(MCPTool).should_receive("from_client").with_args(mock_client).once().and_raise(
            RuntimeError("MCP Client Session has been destroyed.")
        )

        with pytest.raises(RuntimeError, match="MCP Client Session has been destroyed"):
            await get_log_detective_mcp()
