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


async def get_mcp_tools(
    client: ClientSession, filter: Callable[[str], bool] | None = None
) -> list[MCPTool]:
    """Get MCP tools and return them with the active session"""
    tools = await MCPTool.from_client(client)
    if filter:
        tools = [t for t in tools if filter(t.name)]
    return tools


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


def format_event_output(data, event):
    """Format event output to be more readable"""
    # Extract key information based on event type
    if hasattr(data, 'state') and hasattr(data.state, 'iteration'):
        iteration = data.state.iteration
        
        # Get the latest step if available
        latest_step = None
        if hasattr(data.state, 'steps') and data.state.steps:
            latest_step = data.state.steps[-1]
        
        if latest_step:
            if hasattr(latest_step, 'tool') and hasattr(latest_step.tool, '__class__'):
                tool_name = latest_step.tool.__class__.__name__
                
                if tool_name == 'RunShellCommandTool' and hasattr(latest_step, 'input'):
                    command = latest_step.input.get('command', 'Unknown command')
                    print(f"[Iteration {iteration:2d}] Running: {command}")
                    
                    if hasattr(latest_step, 'output') and hasattr(latest_step.output, 'stdout'):
                        stdout = latest_step.output.stdout.strip()
                        if stdout:
                            # Show first few lines of output
                            lines = stdout.split('\n')
                            if len(lines) > 3:
                                print(f"                Output: {lines[0]}")
                                if len(lines) > 1:
                                    print(f"                        ... ({len(lines)-1} more lines)")
                            else:
                                for line in lines:
                                    if line.strip():
                                        print(f"                Output: {line}")
                    
                    if hasattr(latest_step, 'output') and hasattr(latest_step.output, 'stderr'):
                        stderr = latest_step.output.stderr.strip()
                        if stderr:
                            print(f"                Error:  {stderr}")
                            
                elif tool_name == 'ThinkTool' and hasattr(latest_step, 'input'):
                    thoughts = latest_step.input.get('thoughts', '')
                    next_step = latest_step.input.get('next_step', [])
                    if thoughts:
                        # Show first part of thoughts
                        thought_preview = thoughts[:150] + '...' if len(thoughts) > 150 else thoughts
                        print(f"[Iteration {iteration:2d}] Thinking: {thought_preview}")
                    if next_step and isinstance(next_step, list) and next_step:
                        next_preview = next_step[0][:100] + '...' if len(next_step[0]) > 100 else next_step[0]
                        print(f"                Next: {next_preview}")
                else:
                    print(f"[Iteration {iteration:2d}] Tool: {tool_name}")
            else:
                print(f"[Iteration {iteration:2d}] Processing...")
        else:
            print(f"[Iteration {iteration:2d}] {event.name if hasattr(event, 'name') else 'Unknown event'}")
    else:
        # Fallback for other event types
        event_name = event.name if hasattr(event, 'name') else 'Unknown'
        if event_name not in ['start', 'success']:  # Filter out noise
            print(f"[Event] {event_name}")