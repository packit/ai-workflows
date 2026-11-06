"""MCP request metadata adapter for MCP SDK major versions."""

from contextvars import ContextVar
from typing import Any

from ymir.common.request_context import request_metadata

try:
    from mcp.server.lowlevel.server import request_ctx as _mcp_request_ctx
except ImportError:
    _mcp_request_ctx = None

_fallback_request_ctx: ContextVar[Any] = ContextVar("ymir_mcp_request_context")
request_ctx = _mcp_request_ctx or _fallback_request_ctx


def request_meta() -> Any:
    """Return the current MCP request metadata, if a request is active."""
    try:
        context = _mcp_request_ctx.get() if _mcp_request_ctx is not None else _fallback_request_ctx.get()
    except LookupError:
        return request_metadata.get()
    return getattr(context, "meta", context)


async def request_context_middleware(context: Any, call_next: Any) -> Any:
    """Expose MCP 2.x's handler context to tools that need request metadata."""
    token = request_metadata.set(getattr(context, "meta", None))
    try:
        return await call_next(context)
    finally:
        request_metadata.reset(token)


def install_request_context_middleware(server: Any) -> None:
    """Install the MCP 2.x middleware when the native server supports it."""
    middleware = getattr(getattr(server, "_server", None), "middleware", None)
    if isinstance(middleware, list) and request_context_middleware not in middleware:
        middleware.insert(0, request_context_middleware)
