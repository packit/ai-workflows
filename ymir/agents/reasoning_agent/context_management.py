"""Agent-driven conversation context compaction for ReasoningAgent.

The agent authors a durable summary via ``manage_context`` in the same inference
turn as other tools. The tool only schedules compaction; the runner applies it
after ``asyncio.gather`` so parallel tool execution cannot race on memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from beeai_framework.backend import (
    AnyMessage,
    AssistantMessage,
    MessageToolCallContent,
    ToolMessage,
    UserMessage,
)
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ymir.agents.reasoning_agent.types import ReasoningAgentRunState

YMIR_PROTECTED_META_KEY = "ymir_protected"
YMIR_ROLE_META_KEY = "ymir_role"
YMIR_CONTEXT_SUMMARY_META_KEY = "ymir_context_summary"
MANAGE_CONTEXT_TOOL_NAME = "manage_context"


class ManageContextSchema(BaseModel):
    durable_summary: str = Field(
        description=(
            "Concise durable facts still needed for further work: paths, IDs, "
            "current hypothesis, TF/MR identifiers. "
            "You MUST include every unsuccessful approach already tried — what was "
            "attempted, why it failed, and that it must not be retried. "
            "Do not restate the task or system instructions."
        )
    )
    keep_recent_exchanges: int = Field(
        default=1,
        ge=1,
        description=(
            "Number of most recent assistant+tool exchanges to keep verbatim "
            "(including the current turn). Older exchanges are replaced by durable_summary."
        ),
    )


class ManageContextTool(Tool[ManageContextSchema, ToolRunOptions, StringToolOutput]):
    name = MANAGE_CONTEXT_TOOL_NAME
    description = (
        "Compact conversation memory by replacing older tool exchanges with a durable "
        "summary you provide. Call this in the SAME turn as another useful tool when "
        "history contains dead ends, large obsolete dumps, or failed approaches. "
        "Your summary MUST preserve a clear record of every unsuccessful approach "
        "(what was tried, why it failed, do not retry) so you do not repeat them. "
        "Do not call it alone. Never summarize away the task or system instructions."
    )

    def __init__(self, state: ReasoningAgentRunState) -> None:
        super().__init__()
        self._state = state

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", MANAGE_CONTEXT_TOOL_NAME], creator=self)

    @property
    def input_schema(self) -> type[BaseModel]:
        return ManageContextSchema

    async def _run(
        self, input: ManageContextSchema, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        self._state.pending_context_compaction = input
        return StringToolOutput(
            "Context compaction scheduled; it will apply after this turn's tools complete."
        )

    async def clone(self) -> Self:
        tool = self.__class__(state=self._state.model_copy())
        tool.name = self.name
        tool.description = self.description
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool


def partition_exchanges(messages: list[AnyMessage]) -> tuple[list[AnyMessage], list[list[AnyMessage]]]:
    """Split memory into protected task messages and tool/user exchanges.

    Every message tagged with ``YMIR_PROTECTED_META_KEY`` is protected, even
    when memory already contains older unprotected exchanges from a prior
    ``ReasoningAgent.run()`` (e.g. ``save_intermediate_steps=True``).
    An exchange is an assistant message with tool calls plus its following tool
    results, or a standalone user/assistant text message (e.g. a prior context
    summary).
    """
    protected: list[AnyMessage] = []
    rest: list[AnyMessage] = []
    for msg in messages:
        if msg.meta.get(YMIR_PROTECTED_META_KEY):
            protected.append(msg)
        else:
            rest.append(msg)

    exchanges: list[list[AnyMessage]] = []
    current: list[AnyMessage] = []
    for msg in rest:
        if isinstance(msg, AssistantMessage) and msg.get_tool_calls():
            if current:
                exchanges.append(current)
            current = [msg]
        elif isinstance(msg, ToolMessage):
            if current:
                current.append(msg)
            else:
                exchanges.append([msg])
        else:
            if current:
                exchanges.append(current)
                current = []
            exchanges.append([msg])
    if current:
        exchanges.append(current)
    return protected, exchanges


def _assistant_has_provider_visible_output(msg: AssistantMessage) -> bool:
    """Return True if the message has text or tool_calls (not reasoning-only).

    Anthropic/Vertex reject assistant messages whose final block is ``thinking``.
    Extended thinking is round-tripped via signed ``meta['thinking_blocks']``
    (Vertex/Anthropic's format); ``MessageReasoningContent`` in content alone is
    not sent as a valid assistant block. A message with only reasoning content
    (or empty content + ``thinking_blocks``) is therefore an invalid
    thinking-only turn.
    """
    return bool(msg.get_tool_calls() or msg.get_texts())


def strip_manage_context_from_exchange(exchange: list[AnyMessage]) -> list[AnyMessage]:
    """Remove manage_context tool-call/result pairs from a kept exchange.

    Mutates assistant content in place so ``meta['thinking_blocks']`` (signed
    Anthropic thinking) stays attached to the same message object. Drops the
    assistant message entirely when stripping would leave a thinking-only turn
    (no text/tool_calls), which Vertex/Claude reject.
    """
    manage_ids: set[str] = set()
    for msg in exchange:
        if isinstance(msg, AssistantMessage):
            for call in msg.get_tool_calls():
                if call.tool_name == MANAGE_CONTEXT_TOOL_NAME:
                    manage_ids.add(call.id)

    if not manage_ids:
        return list(exchange)

    cleaned: list[AnyMessage] = []
    for msg in exchange:
        if isinstance(msg, AssistantMessage):
            msg.content[:] = [
                content
                for content in msg.content
                if not (
                    isinstance(content, MessageToolCallContent)
                    and content.tool_name == MANAGE_CONTEXT_TOOL_NAME
                )
            ]
            # Drop the assistant if stripping manage_context left only thinking:
            # meta['thinking_blocks'] would still serialize, but with no text or
            # tool_calls Vertex rejects the message as thinking-only.
            if _assistant_has_provider_visible_output(msg):
                cleaned.append(msg)
        elif isinstance(msg, ToolMessage):
            msg.content[:] = [
                content
                for content in msg.content
                if getattr(content, "tool_call_id", None) not in manage_ids
                and getattr(content, "tool_name", None) != MANAGE_CONTEXT_TOOL_NAME
            ]
            if msg.content:
                cleaned.append(msg)
        else:
            cleaned.append(msg)
    return cleaned


def sanitize_assistant_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Drop assistant messages that would serialize as thinking-only."""
    return [
        msg
        for msg in messages
        if not (isinstance(msg, AssistantMessage) and not _assistant_has_provider_visible_output(msg))
    ]


async def apply_pending_context_compaction(state: ReasoningAgentRunState) -> bool:
    """Apply a scheduled compaction to ``state.memory``. Returns True if applied."""
    pending = state.pending_context_compaction
    if pending is None:
        return False

    state.pending_context_compaction = None
    if not isinstance(pending, ManageContextSchema):
        pending = ManageContextSchema.model_validate(
            pending.model_dump() if isinstance(pending, BaseModel) else pending
        )

    protected, exchanges = partition_exchanges(list(state.memory.messages))
    keep_n = pending.keep_recent_exchanges
    to_drop = exchanges[:-keep_n]
    to_keep = exchanges[-keep_n:]

    cleaned_kept: list[AnyMessage] = []
    for exchange in to_keep:
        cleaned_kept.extend(strip_manage_context_from_exchange(exchange))

    new_messages: list[AnyMessage] = list(protected)
    if to_drop:
        new_messages.append(
            UserMessage(
                f"[Context summary — earlier tool traffic was compacted]\n\n{pending.durable_summary}",
                meta={YMIR_CONTEXT_SUMMARY_META_KEY: True},
            )
        )
    new_messages.extend(cleaned_kept)
    new_messages = sanitize_assistant_messages(new_messages)

    state.memory.reset()
    await state.memory.add_many(new_messages)
    return True
