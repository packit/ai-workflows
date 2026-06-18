import logging
import re

from beeai_framework.context import RunContext, RunMiddlewareProtocol
from beeai_framework.emitter import EventMeta
from beeai_framework.tools.events import ToolSuccessEvent

from ymir.tools.privileged.testing_farm import _testing_farm_api_delete

logger = logging.getLogger(__name__)


class TFReservationCleanupMiddleware(RunMiddlewareProtocol):
    """Track Testing Farm reservations and cancel leaked ones on agent crash."""

    def __init__(self) -> None:
        self._reserved: set[str] = set()
        self._cancelled: set[str] = set()

    def bind(self, ctx: RunContext) -> None:
        ctx.emitter.on(
            re.compile(r"^tool\.reserve_testing_farm_machine\.success$"),
            self._on_reserve,
        )
        ctx.emitter.on(
            re.compile(r"^tool\.cancel_testing_farm_request\.success$"),
            self._on_cancel,
        )

    async def _on_reserve(self, event: ToolSuccessEvent, meta: EventMeta) -> None:
        request_id = event.output.result.get("id")
        if request_id:
            self._reserved.add(request_id)
            logger.debug("Tracked TF reservation %s", request_id)

    async def _on_cancel(self, event: ToolSuccessEvent, meta: EventMeta) -> None:
        request_id = event.input.request_id if hasattr(event.input, "request_id") else None
        if request_id:
            self._cancelled.add(request_id)
            logger.debug("Tracked TF cancellation %s", request_id)

    async def cleanup(self) -> None:
        """Cancel any reserved machines that were not explicitly cancelled."""
        leaked = self._reserved - self._cancelled
        for request_id in leaked:
            logger.warning("Cleaning up leaked TF reservation %s", request_id)
            try:
                _testing_farm_api_delete(f"requests/{request_id}")
                logger.info("Successfully cancelled leaked TF reservation %s", request_id)
            except Exception:
                logger.exception("Failed to cancel leaked TF reservation %s", request_id)
