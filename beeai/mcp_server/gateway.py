import os
import inspect

from fastmcp import FastMCP

import gitlab_tools
import jira_tools
import lookaside_tools
import copr_tools


mcp = FastMCP(
    name="MCP Gateway",
    tools=[
        function
        for module in [gitlab_tools, jira_tools, lookaside_tools, copr_tools]
        for name, function in inspect.getmembers(module)
        if (inspect.isfunction(function) or inspect.iscoroutinefunction(function))
        and function.__module__ == module.__name__
        and not name.startswith("_")
    ]
)


if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.getenv("SSE_PORT")))
