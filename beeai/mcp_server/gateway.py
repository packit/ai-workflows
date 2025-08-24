import os
import inspect

from fastmcp import FastMCP

import gitlab_tools
import jira_tools
import lookaside_tools


mcp = FastMCP(
    name="MCP Gateway",
    tools=[
        coroutine
        for module in [gitlab_tools, jira_tools, lookaside_tools]
        for name, coroutine in inspect.getmembers(module, inspect.iscoroutinefunction)
        if coroutine.__module__ == module.__name__
        and not name.startswith("_")
    ]
)


if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.getenv("SSE_PORT", "8000")))
