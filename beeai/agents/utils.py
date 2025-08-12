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
    git_url: str = "https://gitlab.com/redhat/centos-stream/rpms",
    git_user: str = "RHEL Packaging Agent",
    git_email: str = "rhel-packaging-agent@redhat.com",
    dist_git_branch: str = "c9s",
    srpms_basepath: str = "/srpms",
) -> str:
    """Generate Git finalization steps with dry-run support"""
    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    # Common commit steps
    commit_steps = f"""* Add files to commit: {files_to_commit}
            * Create commit with title: "{commit_title}" and author: "{git_user} <{git_email}>"
            * Include JIRA reference: "Resolves: {jira_issue}" in commit body
            * This is the path to the SRPMs: {srpms_basepath}"""

    if dry_run:
        return f"""
        **DRY RUN MODE**: Commit changes locally only - validation and testing still required

        Commit the changes:
            {commit_steps}

        **Important**: In dry-run mode, only commit locally. Do not push or create merge requests.
        **Note**: Dry-run mode does NOT skip validation steps - all validation (rpmlint, Copr builds) must still be performed.
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

