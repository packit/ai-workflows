import inspect
import os

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable, TypeVar

import redis.asyncio as redis
from mcp import ClientSession
from mcp.client.sse import sse_client

from beeai_framework.agents import AgentExecutionConfig
from beeai_framework.tools.mcp import MCPTool


def get_agent_execution_config() -> AgentExecutionConfig:
    return AgentExecutionConfig(
        max_retries_per_step=int(os.getenv("BEEAI_MAX_RETRIES_PER_STEP", 5)),
        total_max_retries=int(os.getenv("BEEAI_TOTAL_MAX_RETRIES", 10)),
        max_iterations=int(os.getenv("BEEAI_MAX_ITERATIONS", 100)),
    )


@asynccontextmanager
async def redis_client(redis_url: str) -> AsyncGenerator[redis.Redis, None]:
    client = redis.Redis.from_url(redis_url)
    await client.ping()
    try:
        yield client
    finally:
        await client.aclose()


T = TypeVar("T")

async def fixAwait(v: T | Awaitable[T]) -> T:
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
    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    # Common commit steps
    commit_steps = f"""* Add files to commit: {files_to_commit}
            * Create commit with title: "{commit_title}"
            * Include JIRA reference: "Resolves: {jira_issue}" in commit body"""

    if dry_run:
        return f"""
        **DRY RUN MODE**: Commit changes locally only

        Commit the changes:
            {commit_steps}

        **Important**: In dry-run mode, only commit locally. Do not push or create merge requests.
        """
    else:
        return f"""
        Commit and push the changes:
            {commit_steps}
            * Push the branch `{branch_name}` to the fork using the `push_to_remote_repository` tool,
              do not use `git push`

        Open a merge request:
            * Open a merge request against {git_url}/{package}
            * Target branch: {dist_git_branch}
        """
