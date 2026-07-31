"""Unit tests for TFReservationCleanupMiddleware."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from beeai_framework.tools.types import JSONToolOutput
from pydantic import BaseModel, Field

from ymir.agents.tf_cleanup_middleware import (
    _CANCEL_SUCCESS,
    _RESERVE_SUCCESS,
    TFReservationCleanupMiddleware,
    _extract_request_id_from_input,
    _extract_request_id_from_output,
)


class _CancelInput(BaseModel):
    request_id: str = Field()


@pytest.mark.parametrize(
    "path",
    [
        "tool.reserve_testing_farm_machine.success",
        "tool.mcp.reserve_testing_farm_machine.success",
    ],
)
def test_reserve_success_matcher(path):
    assert _RESERVE_SUCCESS.match(path)


@pytest.mark.parametrize(
    "path",
    [
        "tool.cancel_testing_farm_request.success",
        "tool.mcp.cancel_testing_farm_request.success",
    ],
)
def test_cancel_success_matcher(path):
    assert _CANCEL_SUCCESS.match(path)


def test_matchers_reject_unrelated_tools():
    assert _RESERVE_SUCCESS.match("tool.mcp.clone_repository.success") is None
    assert _CANCEL_SUCCESS.match("tool.mcp.reserve_testing_farm_machine.success") is None


def test_extract_request_id_from_flat_and_nested_output():
    assert _extract_request_id_from_output(JSONToolOutput({"id": "req-1"})) == "req-1"
    assert _extract_request_id_from_output(JSONToolOutput({"result": {"id": "req-2"}})) == "req-2"
    assert _extract_request_id_from_output(JSONToolOutput({"id": "dry-run-reservation"})) is None
    assert _extract_request_id_from_output(JSONToolOutput({"message": "no id"})) is None


def test_extract_request_id_from_input_model_or_dict():
    assert _extract_request_id_from_input(SimpleNamespace(request_id="req-3")) == "req-3"
    assert _extract_request_id_from_input({"request_id": "req-4"}) == "req-4"
    assert _extract_request_id_from_input(_CancelInput(request_id="req-5")) == "req-5"
    assert _extract_request_id_from_input({"request_id": "dry-run-reservation"}) is None
    assert _extract_request_id_from_input({}) is None


@pytest.mark.asyncio
async def test_middleware_tracks_mcp_reserve_and_cancel():
    mw = TFReservationCleanupMiddleware()
    ctx = MagicMock()
    handlers: dict[str, object] = {}

    def on(matcher, handler):
        handlers[matcher.pattern] = handler

    ctx.emitter.on.side_effect = on
    mw.bind(ctx)

    assert _RESERVE_SUCCESS.pattern in handlers
    assert _CANCEL_SUCCESS.pattern in handlers

    await mw._on_reserve(
        SimpleNamespace(output=JSONToolOutput({"id": "req-100"}), input={}),
        MagicMock(),
    )
    assert mw._reserved == {"req-100"}

    await mw._on_cancel(
        SimpleNamespace(output=JSONToolOutput({"cancelled": True}), input=_CancelInput(request_id="req-100")),
        MagicMock(),
    )
    assert mw._cancelled == {"req-100"}

    with patch("ymir.agents.tf_cleanup_middleware.run_tool") as cancel:
        await mw.cleanup(available_tools=[])
        cancel.assert_not_called()


@pytest.mark.asyncio
async def test_middleware_cleanup_cancels_leaked_reservations():
    mw = TFReservationCleanupMiddleware()
    mw._reserved.add("req-leak")
    tools = [object()]

    with patch("ymir.agents.tf_cleanup_middleware.run_tool") as cancel:
        await mw.cleanup(available_tools=tools)
        cancel.assert_called_once_with(
            "cancel_testing_farm_request",
            request_id="req-leak",
            available_tools=tools,
        )


@pytest.mark.asyncio
async def test_middleware_ignores_dry_run_reservation_id():
    mw = TFReservationCleanupMiddleware()
    await mw._on_reserve(
        SimpleNamespace(output=JSONToolOutput({"id": "dry-run-reservation"}), input={}),
        MagicMock(),
    )
    assert mw._reserved == set()
