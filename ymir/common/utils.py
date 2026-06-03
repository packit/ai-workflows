"""
Common utility functions shared across the BeeAI system.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import koji
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.mcp import MCPTool
from beeai_framework.tools.types import JSONToolOutput, StringToolOutput
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import CallToolResult, TextContent
from specfile.utils import EVR

from ymir.common.constants import BREWHUB_URL

logger = logging.getLogger(__name__)


class _MetaInjectingSession:
    """Transparent wrapper around ``ClientSession`` that injects ``meta``
    into every ``call_tool`` invocation.

    All other attribute accesses are forwarded to the underlying session so
    that ``MCPTool.from_session`` (which calls ``list_tools``, ``initialize``,
    etc.) keeps working unchanged.
    """

    def __init__(self, session: ClientSession, meta: dict[str, Any]) -> None:
        self._session = session
        self._meta = meta

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: Any = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        merged = {**self._meta, **(meta or {})}
        return await self._session.call_tool(
            name,
            arguments,
            read_timeout_seconds=read_timeout_seconds,
            progress_callback=progress_callback,
            meta=merged,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


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
    call_meta: dict[str, Any] | None = None,
) -> AsyncGenerator[list[MCPTool]]:
    """Connect to an MCP gateway and yield the available tools.

    Args:
        sse_url: SSE endpoint of the MCP gateway.
        filter: Optional predicate to keep only matching tool names.
        max_retries: How many connection attempts before giving up.
        retry_delay: Seconds between retries.
        call_meta: Optional dict injected as MCP ``_meta`` on every
            ``call_tool`` invocation.  Use this to propagate context
            such as ``{"jira_issue": "RHEL-12345"}`` so that the
            gateway can scope operations per-caller.
    """
    connected = False
    for attempt in range(max_retries):
        try:
            async with sse_client(sse_url) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                effective_session: Any = session
                if call_meta:
                    effective_session = _MetaInjectingSession(session, call_meta)
                tools = await MCPTool.from_session(effective_session)
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


async def get_latest_candidate_build(package: str, dist_git_branch: str) -> tuple[EVR, str]:
    candidate_tags = [
        f"{dist_git_branch}-candidate",
        f"{dist_git_branch}-z-candidate",
    ]

    def get_latest_build(tag):
        builds = koji.ClientSession(BREWHUB_URL).listTagged(
            package=package,
            tag=tag,
            latest=True,
            inherit=True,
            strict=False,
        )
        if not builds:
            return None
        [build] = builds
        return build

    results = await asyncio.gather(
        *(asyncio.to_thread(get_latest_build, tag) for tag in candidate_tags),
    )
    latest = None
    for build in results:
        if build is None:
            continue
        evr = EVR(
            epoch=build["epoch"] or 0,
            version=build["version"],
            release=build["release"],
        )
        if latest is None or latest[0] < evr:
            latest = (evr, build["build_id"])
    if latest is None:
        raise RuntimeError(f"There are no builds of {package} in {' or '.join(candidate_tags)}")
    evr, build_id = latest
    session = koji.ClientSession(BREWHUB_URL)
    metadata = await asyncio.to_thread(session.getBuild, build_id, strict=True)
    source_ref = metadata["source"].split("#")[-1]
    return evr, source_ref
