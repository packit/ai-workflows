import logging
import os
import inspect
import functools

from fastmcp import FastMCP

import copr_tools
import distgit_tools
import gitlab_tools
import jira_tools
import lookaside_tools


logger = logging.getLogger(__name__)


def log_tool_call(func):
    """Decorator to log tool calls with their arguments."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        tool_name = func.__name__
        logger.info(f"Tool called: {tool_name}")
        logger.info(f"Tool arguments: args={args}, kwargs={kwargs}")
        try:
            result = await func(*args, **kwargs)
            logger.info(f"Tool {tool_name} completed successfully")
            return result
        except Exception as e:
            logger.error(f"Tool {tool_name} failed with error: {e}")
            raise
    return wrapper


# Collect all tools and wrap them with logging
tools = [
    log_tool_call(coroutine)
    for module in [copr_tools, distgit_tools, gitlab_tools, jira_tools, lookaside_tools]
    for name, coroutine in inspect.getmembers(module, inspect.iscoroutinefunction)
    if coroutine.__module__ == module.__name__
    and not name.startswith("_")
]

mcp = FastMCP(
    name="MCP Gateway",
    tools=tools
)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("FastMCP").handlers = [logging.StreamHandler()]
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.getenv("SSE_PORT", "8000")))
