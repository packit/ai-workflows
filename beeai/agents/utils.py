import os

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable

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


async def run_tool(
    tool: str | Tool,
    available_tools: list[Tool] | None = None,
    **kwargs: dict[str, Any]
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
async def redis_client(redis_url: str) -> AsyncGenerator[redis.Redis, None]:
    client = redis.Redis.from_url(redis_url)
    await client.ping()
    try:
        yield client
    finally:
        await client.aclose()


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


def get_git_finalization_steps(
    package: str,
    jira_issue: str,
    commit_title: str,
    files_to_commit: str,
    branch_name: str,
    git_url: str = "https://gitlab.com/redhat/centos-stream/rpms",
    dist_git_branch: str = "c9s",
) -> str:
    """Generate Git finalization steps with dry-run support"""

    # Common commit steps
    commit_steps = f"""* Add files to commit: {files_to_commit}
            * Create commit with title: "{commit_title}"
            * Include JIRA reference: "Resolves: {jira_issue}" in commit body"""

    return f"""
    Commit the changes:
        {commit_steps}
    """
