import re
from datetime import datetime
from typing import Any

from beeai_framework.context import (
    RunContext,
    RunContextFinishEvent,
    RunContextStartEvent,
    RunMiddlewareProtocol,
)
from beeai_framework.emitter import EmitterOptions, EventMeta
from beeai_framework.emitter.utils import create_internal_event_matcher


class MetricsMiddleware(RunMiddlewareProtocol):
    def __init__(self) -> None:
        self.start_time: datetime | None = None
        self.end_time: datetime | None = None
        self.tool_calls: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.agent_name: str = ""

    def bind(self, ctx: RunContext) -> None:
        meta = getattr(ctx.instance, "meta", None)
        self.agent_name = getattr(meta, "name", "") if meta else ""
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
        ctx.emitter.on(
            re.compile(r"^tool\..+\.start$"),
            self._on_tool_start,
        )

    async def _on_run_context_start(self, event: RunContextStartEvent, meta: EventMeta) -> None:
        self.start_time = datetime.now()

    async def _on_run_context_finish(self, event: RunContextFinishEvent, meta: EventMeta) -> None:
        self.end_time = datetime.now()
        output = event.output
        state = getattr(output, "state", None)
        if state is None:
            return
        usage = getattr(state, "usage", None)
        if usage is None:
            return
        self.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens = getattr(usage, "completion_tokens", 0) or 0

    async def _on_tool_start(self, event: Any, meta: EventMeta) -> None:
        self.tool_calls += 1

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0

    def get_metrics(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "duration": self.duration,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
