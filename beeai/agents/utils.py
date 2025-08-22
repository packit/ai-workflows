import asyncio
import inspect
import logging
import os
import shlex
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, TypeVar, Tuple

import redis.asyncio as redis
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent

from beeai_framework.agents import AgentExecutionConfig
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.mcp import MCPTool


def get_agent_execution_config() -> AgentExecutionConfig:
    return AgentExecutionConfig(
        max_retries_per_step=int(os.getenv("BEEAI_MAX_RETRIES_PER_STEP", 5)),
        total_max_retries=int(os.getenv("BEEAI_TOTAL_MAX_RETRIES", 10)),
        max_iterations=int(os.getenv("BEEAI_MAX_ITERATIONS", 100)),
    )


async def run_subprocess(
    cmd: str | list[str],
    shell: bool = False,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> Tuple[int, str | None, str | None]:
    kwargs = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = os.environ.copy()
        kwargs["env"].update(env)
    if shell:
        if not isinstance(cmd, str):
            cmd = shlex.join(cmd)
        proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
    else:
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        proc = await asyncio.create_subprocess_exec(cmd[0], *cmd[1:], **kwargs)
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode,
        stdout.decode() if stdout else None,
        stderr.decode() if stderr else None,
    )


async def check_subprocess(
    cmd: str | list[str],
    shell: bool = False,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> Tuple[str | None, str | None]:
    exit_code, stdout, stderr = await run_subprocess(cmd, shell, cwd, env)
    if exit_code:
        raise subprocess.CalledProcessError(exit_code, cmd, stdout, stderr)
    return stdout, stderr


async def run_tool(
    tool: str | Tool,
    available_tools: list[Tool] | None = None,
    **kwargs: Any,
) -> str | dict:
    if isinstance(tool, str):
        resolved_tool = next(t for t in available_tools or [] if t.name == tool)
    else:
        resolved_tool = tool
    output = await resolved_tool.run(input=kwargs).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.to_json_safe()
    if isinstance(result, list):
        [result] = result
    if isinstance(result, TextContent):
        result = result.text
    return result


@asynccontextmanager
async def redis_client(redis_url: str) -> AsyncGenerator[redis.Redis, None]:
    client = redis.Redis.from_url(redis_url)
    await client.ping()
    try:
        yield client
    finally:
        await client.aclose()


T = TypeVar("T")

async def fix_await(v: T | Awaitable[T]) -> T:
    """
    Work around typing problems in the asyncio redis client.

    Typing for the asyncio redis client is messed up, and functions
    return `T | Awaitable[T]` instead of `T`. This function
    fixes the type error by asserting that the value is awaitable
    before awaiting it.

    For a proper fix, see: https://github.com/redis/redis-py/pull/3619


    Usage: `await fixAwait(redis.get("key"))`
    """
    assert inspect.isawaitable(v)
    return await v


@asynccontextmanager
async def mcp_tools(
    sse_url: str, filter: Callable[[str], bool] | None = None
) -> AsyncGenerator[list[MCPTool], None]:
    async with sse_client(sse_url) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await MCPTool.from_client(session)
        if filter:
            tools = [t for t in tools if filter(t.name)]
        yield tools
