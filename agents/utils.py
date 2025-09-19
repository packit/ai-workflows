import asyncio
import inspect
import logging
import os
import shlex
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, TypeVar, Tuple

from beeai_framework.backend import ChatModel, ChatModelParameters
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent
from pydantic import BaseModel

from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.template import PromptTemplate
from beeai_framework.tools import Tool
from beeai_framework.tools.mcp import MCPTool


def get_chat_model() -> ChatModel:
    return ChatModel.from_name(
        os.environ["CHAT_MODEL"],
        # lowering the temperature makes the model stop backporting too soon
        # but should yield more predictable results
        # similar for top_p (tried 0.5)
        options=ChatModelParameters(temperature=0.6),
        timeout=1200,
    )


def get_agent_execution_config() -> dict[str, int]:
    return dict(
        max_retries_per_step=int(os.getenv("BEEAI_MAX_RETRIES_PER_STEP", 5)),
        # 10 can easily be depleted by one of our tools failing 10 times
        # i.e. str_replace, view, etc.
        total_max_retries=int(os.getenv("BEEAI_TOTAL_MAX_RETRIES", 25)),
        # 140 is not enough for a more complex rebase
        # 140 is not enough for a more complex rebase or for a backport
        # with 19 commits and numerous merge conflicts, so we have 255 now
        max_iterations=int(os.getenv("BEEAI_MAX_ITERATIONS", 255)),
    )


def render_prompt(template: str, input: BaseModel) -> str:
    """Renders a prompt template with the specified input, according to its schema."""
    return PromptTemplate(template=template, schema=type(input)).render(input)


def get_absolute_path(path: Path, tool: Tool) -> Path:
    if path.is_absolute():
        return path
    cwd = (tool.options or {}).get("working_directory") or Path.cwd()
    return Path(cwd) / path


async def run_subprocess(
    cmd: str | list[str],
    shell: bool = False,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> Tuple[int, str | None, str | None]:
    """Run a subprocess and return the exit code, stdout, and stderr."""
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
        tool = next(t for t in available_tools or [] if t.name == tool)
    output = await tool.run(input=kwargs).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.to_json_safe()
    if isinstance(result, list):
        [result] = result
    if isinstance(result, TextContent):
        result = result.text
    return result


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
