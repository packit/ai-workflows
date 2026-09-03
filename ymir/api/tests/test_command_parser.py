"""Unit tests for the command parser."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from ymir.api import command_parser


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure each test starts with an empty command registry."""
    saved = dict(command_parser._REGISTRY)
    command_parser._REGISTRY.clear()
    yield
    command_parser._REGISTRY.clear()
    command_parser._REGISTRY.update(saved)


def _parse_response_body(resp: web.Response) -> dict:
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_dispatch_calls_registered_handler():
    handler = AsyncMock(return_value=web.json_response({"ok": True}, status=201))
    command_parser.register("hello", handler)

    request = object()
    resp = await command_parser.dispatch("hello world", request)

    handler.assert_awaited_once_with(["world"], request)
    assert resp.status == 201


@pytest.mark.asyncio
async def test_dispatch_unknown_command():
    resp = await command_parser.dispatch("no-such-command arg1", object())
    assert resp.status == 400
    body = _parse_response_body(resp)
    assert "unknown command" in body["error"]


@pytest.mark.asyncio
async def test_dispatch_empty_command():
    resp = await command_parser.dispatch("", object())
    assert resp.status == 400
    body = _parse_response_body(resp)
    assert "empty command" in body["error"]


@pytest.mark.asyncio
async def test_dispatch_case_insensitive():
    handler = AsyncMock(return_value=web.json_response({"ok": True}))
    command_parser.register("greet", handler)

    request = object()
    await command_parser.dispatch("GREET Alice", request)
    handler.assert_awaited_once_with(["Alice"], request)


@pytest.mark.asyncio
async def test_dispatch_handles_quoted_args():
    handler = AsyncMock(return_value=web.json_response({"ok": True}))
    command_parser.register("echo", handler)

    request = object()
    await command_parser.dispatch('echo "hello world" foo', request)
    handler.assert_awaited_once_with(["hello world", "foo"], request)


@pytest.mark.asyncio
async def test_dispatch_bad_quoting():
    resp = await command_parser.dispatch('echo "unterminated', object())
    assert resp.status == 400
    body = _parse_response_body(resp)
    assert "failed to parse" in body["error"]
