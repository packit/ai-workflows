import logging
import re
from typing import Any

from beeai_framework.context import RunContext, RunMiddlewareProtocol
from beeai_framework.emitter import EventMeta
from beeai_framework.tools.events import ToolSuccessEvent

from ymir.common.utils import run_tool

logger = logging.getLogger(__name__)

_DRY_RUN_RESERVATION_ID = "dry-run-reservation"

# MCP-proxied tools emit ``tool.mcp.<name>.success``; direct tools use ``tool.<name>.success``.
_RESERVE_SUCCESS = re.compile(r"^tool\.(?:mcp\.)?reserve_testing_farm_machine\.success$")
_CANCEL_SUCCESS = re.compile(r"^tool\.(?:mcp\.)?cancel_testing_farm_request\.success$")


def _extract_request_id_from_output(output: Any) -> str | None:
    """Pull a reservation id from a ToolSuccessEvent output payload."""
    result = getattr(output, "result", output)
    if not isinstance(result, dict):
        return None
    request_id = result.get("id")
    if request_id is None and isinstance(result.get("result"), dict):
        request_id = result["result"].get("id")
    if not request_id or request_id == _DRY_RUN_RESERVATION_ID:
        return None
    return str(request_id)


def _extract_request_id_from_input(tool_input: Any) -> str | None:
    """Pull request_id from cancel tool input (model or dict)."""
    if tool_input is None:
        return None
    request_id = None
    if isinstance(tool_input, dict):
        request_id = tool_input.get("request_id")
    else:
        request_id = getattr(tool_input, "request_id", None)
        if request_id is None and hasattr(tool_input, "model_dump"):
            try:
                dumped = tool_input.model_dump()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                request_id = dumped.get("request_id")
    if not request_id or request_id == _DRY_RUN_RESERVATION_ID:
        return None
    return str(request_id)


class TFReservationCleanupMiddleware(RunMiddlewareProtocol):
    """Track Testing Farm reservations and cancel leaked ones on agent crash.

    Cancellation goes through the MCP gateway tool (same path as the agent),
    because ``TESTING_FARM_API_TOKEN`` lives on the gateway, not the agent.
    """

    def __init__(self) -> None:
        self._reserved: set[str] = set()
        self._cancelled: set[str] = set()

    def bind(self, ctx: RunContext) -> None:
        ctx.emitter.on(_RESERVE_SUCCESS, self._on_reserve)
        ctx.emitter.on(_CANCEL_SUCCESS, self._on_cancel)

    async def _on_reserve(self, event: ToolSuccessEvent, meta: EventMeta) -> None:
        request_id = _extract_request_id_from_output(event.output)
        if request_id:
            self._reserved.add(request_id)
            logger.debug("Tracked TF reservation %s", request_id)

    async def _on_cancel(self, event: ToolSuccessEvent, meta: EventMeta) -> None:
        request_id = _extract_request_id_from_input(event.input)
        if request_id:
            self._cancelled.add(request_id)
            logger.debug("Tracked TF cancellation %s", request_id)

    async def cleanup(self, available_tools: list) -> None:
        """Cancel any reserved machines that were not explicitly cancelled."""
        leaked = self._reserved - self._cancelled
        for request_id in leaked:
            logger.warning("Cleaning up leaked TF reservation %s", request_id)
            try:
                await run_tool(
                    "cancel_testing_farm_request",
                    request_id=request_id,
                    available_tools=available_tools,
                )
                logger.info("Successfully cancelled leaked TF reservation %s", request_id)
            except Exception:
                logger.exception("Failed to cancel leaked TF reservation %s", request_id)
