import os

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

import redis.asyncio as redis
from mcp import ClientSession
from mcp.client.sse import sse_client

from beeai_framework.tools.mcp import MCPTool


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
    git_user: str = "RHEL Packaging Agent",
    git_email: str = "rhel-packaging-agent@redhat.com",
    git_url: str = "https://gitlab.com/redhat/centos-stream/rpms",
    dist_git_branch: str = "c9s",
) -> str:
    """Generate Git finalization steps with dry-run support"""
    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    # Common commit steps
    git_config_steps = f"""* Configure git user: `git config user.name "{git_user}"`
            * Configure git email: `git config user.email "{git_email}"`"""

    commit_steps = f"""* Add files to commit: {files_to_commit}
            * Create commit with title: "{commit_title}"
            * Include JIRA reference: "Resolves: {jira_issue}" in commit body"""

    if dry_run:
        return f"""
        **DRY RUN MODE**: Commit changes locally only

        Commit the changes:
            {git_config_steps}
            {commit_steps}

        **Important**: In dry-run mode, only commit locally. Do not push or create merge requests.
        """
    else:
        return f"""
        Commit and push the changes:
            {git_config_steps}
            {commit_steps}
            * Push the branch `{branch_name}` to the fork

        Open a merge request:
            * Open a merge request against {git_url}/{package}
            * Target branch: {dist_git_branch}
        """
