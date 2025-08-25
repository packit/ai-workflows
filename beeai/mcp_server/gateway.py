import logging
import os
import inspect

from fastmcp import FastMCP

import copr_tools
import gitlab_tools
import jira_tools
import lookaside_tools


mcp = FastMCP(
    name="MCP Gateway",
    tools=[
        coroutine
        for module in [copr_tools, gitlab_tools, jira_tools, lookaside_tools]
        for name, coroutine in inspect.getmembers(module, inspect.iscoroutinefunction)
        if coroutine.__module__ == module.__name__
        and not name.startswith("_")
    ]
)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.getenv("SSE_PORT", "8000")))
