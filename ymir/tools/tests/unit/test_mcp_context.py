from types import SimpleNamespace

import pytest

from ymir.common.request_context import request_metadata
from ymir.tools.mcp_context import (
    install_request_context_middleware,
    request_context_middleware,
    request_ctx,
    request_meta,
)


def test_request_meta_reads_fallback_context() -> None:
    metadata = SimpleNamespace(jira_issue="RHEL-123")
    token = request_ctx.set(SimpleNamespace(meta=metadata))
    try:
        assert request_meta() is metadata
    finally:
        request_ctx.reset(token)


def test_request_meta_returns_none_without_context() -> None:
    assert request_meta() is None


@pytest.mark.asyncio
async def test_request_context_middleware_sets_and_restores_neutral_context() -> None:
    metadata = SimpleNamespace(jira_issue="RHEL-123")
    context = SimpleNamespace(meta=metadata)

    async def call_next(received: object) -> str:
        assert received is context
        assert request_metadata.get() is metadata
        return "ok"

    assert await request_context_middleware(context, call_next) == "ok"
    assert request_metadata.get() is None


def test_install_request_context_middleware_is_idempotent() -> None:
    server = SimpleNamespace(_server=SimpleNamespace(middleware=[]))

    install_request_context_middleware(server)
    install_request_context_middleware(server)

    assert server._server.middleware == [request_context_middleware]
