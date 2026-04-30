"""
Common utility functions shared across the BeeAI system.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.mcp import MCPTool
from beeai_framework.tools.types import JSONToolOutput, StringToolOutput
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent

logger = logging.getLogger(__name__)


def get_absolute_path(path: Path, tool: Tool) -> Path:
    if path.is_absolute():
        return path
    cwd = (tool.options or {}).get("working_directory") or Path.cwd()
    return Path(cwd) / path


async def run_tool(
    tool: str | Tool,
    available_tools: list[Tool] | None = None,
    **kwargs: Any,
) -> str | dict | list:
    if isinstance(tool, str):
        tool = next(t for t in available_tools or [] if t.name == tool)
    output = await tool.run(input=kwargs).middleware(GlobalTrajectoryMiddleware(pretty=True))
    match output:
        case StringToolOutput():
            result = output.get_text_content()
        case JSONToolOutput():
            result = output.to_json_safe()
        case _:
            result = str(output)
    if isinstance(result, list):
        return [_unpack_tool_result(item) for item in result]
    return _unpack_tool_result(result)


def _unpack_tool_result(result: Any) -> Any:
    if isinstance(result, TextContent):
        result = result.text
    if isinstance(result, dict) and len(result) == 1 and "result" in result:
        result = result["result"]

    # loads twice here is neccessary, because beeai mcp server unfortunately wraps the
    # JSON object twice
    # this has been fixed in BeeAI 0.1.58
    # FIXME: Once BeeAI is updated remove this workaround
    # Only attempt JSON decoding on strings — dicts/lists are already unpacked
    if isinstance(result, str):
        try:
            result = json.loads(result)
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            pass

    return result


def _is_connection_error(exc: Exception) -> bool:
    if isinstance(exc, ExceptionGroup):
        return any(_is_connection_error(e) for e in exc.exceptions)
    return isinstance(exc, (httpx.ConnectError, ConnectionError, OSError))


@asynccontextmanager
async def mcp_tools(
    sse_url: str,
    filter: Callable[[str], bool] | None = None,
    max_retries: int = 10,
    retry_delay: float = 3.0,
) -> AsyncGenerator[list[MCPTool]]:
    connected = False
    for attempt in range(max_retries):
        try:
            async with sse_client(sse_url) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = await MCPTool.from_client(session)
                if filter:
                    tools = [t for t in tools if filter(t.name)]
                connected = True
                yield tools
                return
        except Exception as e:
            if not connected and _is_connection_error(e) and attempt < max_retries - 1:
                logger.warning(
                    "MCP gateway not ready, retrying in %.0fs (attempt %d/%d)...",
                    retry_delay,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(retry_delay)
                continue
            raise
