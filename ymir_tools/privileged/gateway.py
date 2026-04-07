import logging
import os
import inspect
import functools
import re

from fastmcp import FastMCP

import copr_tools
import distgit_tools
import gitlab_tools
import jira_tools
import lookaside_tools


logger = logging.getLogger(__name__)

# Patterns that match common credential formats in log output
_REDACT_PATTERNS = [
    # GitLab PAT
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    # Anthropic API key
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    # Google API key
    re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"),
    # Bearer tokens in URLs or strings
    re.compile(r"oauth2:[^@\s]+@"),
    # Testing Farm API tokens (UUID format)
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE),
    # Jira Cloud API tokens (ATATT3x... pattern)
    re.compile(r"ATATT3x[A-Za-z0-9_-]{20,}"),
    # Base64 Authorization headers
    re.compile(r"Basic [A-Za-z0-9+/=]{20,}"),
    # Generic long hex/base64 tokens (e.g. Jira PATs)
    re.compile(r"(?:token|key|password|secret|credential)[\"'=:\s]+[A-Za-z0-9+/=_-]{20,}['\"\s]*", re.IGNORECASE),
]


def _redact(text: str) -> str:
    """Replace credential-like patterns in text with [REDACTED]."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def log_tool_call(func):
    """Decorator to log tool calls with their arguments.

    Sensitive values (tokens, keys, credentials) are redacted from log output.
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        tool_name = func.__name__
        logger.info(f"Tool called: {tool_name}")
        logger.info("Tool arguments: args=%s, kwargs=%s",
                     _redact(str(args)), _redact(str(kwargs)))
        try:
            result = await func(*args, **kwargs)
            logger.info(f"Tool {tool_name} completed successfully")
            return result
        except Exception as e:
            logger.error("Tool %s failed with error: %s",
                         tool_name, _redact(str(e)))
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
