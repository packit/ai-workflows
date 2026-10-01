"""Unit tests for the command parser."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from flexmock import flexmock

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
    async def _mock_json_response(*_args, **_kwargs):
        return web.json_response({"ok": True}, status=201)

    request = object()
    handler = flexmock()
    handler.should_receive("handle").with_args(["world"], request).replace_with(_mock_json_response).once()

    command_parser.register("hello", handler.handle)
    resp = await command_parser.dispatch("hello world", request)

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
    async def _mock_json_response(*_args, **_kwargs):
        return web.json_response({"ok": True})

    request = object()
    handler = flexmock()
    handler.should_receive("handle").with_args(["Alice"], request).replace_with(_mock_json_response).once()

    command_parser.register("greet", handler.handle)
    await command_parser.dispatch("GREET Alice", request)


@pytest.mark.asyncio
async def test_dispatch_handles_quoted_args():
    async def _mock_json_response(*_args, **_kwargs):
        return web.json_response({"ok": True})

    request = object()
    handler = flexmock()
    handler.should_receive("handle").with_args(["hello world", "foo"], request).replace_with(
        _mock_json_response
    ).once()

    command_parser.register("echo", handler.handle)
    await command_parser.dispatch('echo "hello world" foo', request)


@pytest.mark.asyncio
async def test_dispatch_bad_quoting():
    resp = await command_parser.dispatch('echo "unterminated', object())
    assert resp.status == 400
    body = _parse_response_body(resp)
    assert "failed to parse" in body["error"]
