from datetime import datetime

from beeai_framework.context import (
    RunContextStartEvent,
    RunContextFinishEvent,
    RunMiddlewareProtocol,
    RunContext
)
from beeai_framework.emitter import EmitterOptions, EventMeta
from beeai_framework.emitter.utils import create_internal_event_matcher


class MetricsMiddleware(RunMiddlewareProtocol):
    def __init__(self):
        self.start_time: datetime | None = None
        self.end_time: datetime | None = None
        self.tool_calls: int = 0

    def bind(self, ctx: RunContext) -> None:
        ctx.emitter.on(
            create_internal_event_matcher("start", ctx.instance),
            self._on_run_context_start,
            EmitterOptions(is_blocking=True, priority=1),
        )
        ctx.emitter.on(
            create_internal_event_matcher("finish", ctx.instance),
            self._on_run_context_finish,
            EmitterOptions(is_blocking=True, priority=1),
        )

    async def _on_run_context_start(self, event: RunContextStartEvent, meta: EventMeta):
        self.start_time = datetime.now()

    async def _on_run_context_finish(self, event: RunContextFinishEvent, meta: EventMeta):
        self.end_time = datetime.now()

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0

    def get_metrics(self) -> dict:
        return {"duration": self.duration}
