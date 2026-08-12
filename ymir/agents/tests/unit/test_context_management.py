"""Unit tests for ReasoningAgent context compaction."""

from unittest.mock import MagicMock

import pytest
from beeai_framework.backend import (
    AssistantMessage,
    MessageToolCallContent,
    MessageToolResultContent,
    ToolMessage,
    UserMessage,
)
from beeai_framework.memory import UnconstrainedMemory

from ymir.agents.reasoning_agent.context_management import (
    MANAGE_CONTEXT_TOOL_NAME,
    YMIR_CONTEXT_SUMMARY_META_KEY,
    YMIR_PROTECTED_META_KEY,
    YMIR_ROLE_META_KEY,
    ManageContextSchema,
    ManageContextTool,
    apply_pending_context_compaction,
    partition_exchanges,
    strip_manage_context_from_exchange,
)
from ymir.agents.reasoning_agent.types import ReasoningAgentRunState


def _tool_call(tool_name: str, call_id: str, args: str = "{}") -> MessageToolCallContent:
    return MessageToolCallContent(type="tool-call", id=call_id, tool_name=tool_name, args=args)


def _tool_result(tool_name: str, call_id: str, result: str = "ok") -> ToolMessage:
    return ToolMessage(MessageToolResultContent(tool_name=tool_name, tool_call_id=call_id, result=result))


def _exchange(tool_name: str, call_id: str, result: str = "ok") -> list:
    return [
        AssistantMessage(_tool_call(tool_name, call_id)),
        _tool_result(tool_name, call_id, result),
    ]


def _task_message(text: str = "Your task: write a reproducer") -> UserMessage:
    return UserMessage(
        text,
        meta={YMIR_PROTECTED_META_KEY: True, YMIR_ROLE_META_KEY: "task"},
    )


def _state_with_messages(messages: list) -> ReasoningAgentRunState:
    memory = UnconstrainedMemory()
    # UnconstrainedMemory.add is async; populate via internal list for sync helpers
    memory.messages.extend(messages)
    return ReasoningAgentRunState(answer=None, result=None, memory=memory, steps=[], iteration=1)


def test_partition_exchanges_keeps_protected_prefix():
    task = _task_message()
    ex1 = _exchange("view", "c1", "file contents")
    ex2 = _exchange("shell", "c2", "failed")
    protected, exchanges = partition_exchanges([task, *ex1, *ex2])
    assert protected == [task]
    assert len(exchanges) == 2
    assert exchanges[0] == ex1
    assert exchanges[1] == ex2


def test_partition_exchanges_protects_all_tagged_task_messages():
    task1 = _task_message("Your task: first turn")
    ex1 = _exchange("view", "c1", "file contents")
    task2 = _task_message("Your task: second turn")
    ex2 = _exchange("shell", "c2", "failed")
    protected, exchanges = partition_exchanges([task1, *ex1, task2, *ex2])
    assert protected == [task1, task2]
    assert len(exchanges) == 2
    assert exchanges[0] == ex1
    assert exchanges[1] == ex2


def test_strip_manage_context_from_exchange():
    assistant = AssistantMessage(
        [
            _tool_call("view", "c1"),
            _tool_call(MANAGE_CONTEXT_TOOL_NAME, "c2", '{"durable_summary":"keep me"}'),
        ]
    )
    thinking_meta = {"thinking_blocks": [{"type": "thinking", "thinking": "x", "signature": "sig"}]}
    assistant.meta.update(thinking_meta)
    exchange = [
        assistant,
        _tool_result("view", "c1", "data"),
        _tool_result(MANAGE_CONTEXT_TOOL_NAME, "c2", "scheduled"),
    ]
    cleaned = strip_manage_context_from_exchange(exchange)
    assert len(cleaned) == 2
    assert cleaned[0] is assistant  # in-place; preserves thinking_blocks meta
    assert cleaned[0].meta.get("thinking_blocks") == thinking_meta["thinking_blocks"]
    assert isinstance(cleaned[0], AssistantMessage)
    assert [c.tool_name for c in cleaned[0].get_tool_calls()] == ["view"]
    assert isinstance(cleaned[1], ToolMessage)
    assert cleaned[1].content[0].tool_name == "view"


def test_strip_manage_context_keeps_sibling_results_in_batched_tool_message():
    assistant = AssistantMessage(
        [
            _tool_call("view", "c1"),
            _tool_call(MANAGE_CONTEXT_TOOL_NAME, "c2", '{"durable_summary":"keep me"}'),
        ]
    )
    batched = ToolMessage(
        [
            MessageToolResultContent(tool_name="view", tool_call_id="c1", result="data"),
            MessageToolResultContent(
                tool_name=MANAGE_CONTEXT_TOOL_NAME,
                tool_call_id="c2",
                result="scheduled",
            ),
        ]
    )
    cleaned = strip_manage_context_from_exchange([assistant, batched])
    assert len(cleaned) == 2
    assert [c.tool_name for c in cleaned[0].get_tool_calls()] == ["view"]
    assert len(cleaned[1].content) == 1
    assert cleaned[1].content[0].tool_name == "view"
    assert cleaned[1].content[0].result == "data"


def test_strip_drops_thinking_only_assistant_after_solo_manage_context():
    from beeai_framework.backend.message import MessageReasoningContent

    assistant = AssistantMessage(
        [
            MessageReasoningContent(text="pondering"),
            _tool_call(MANAGE_CONTEXT_TOOL_NAME, "c1", '{"durable_summary":"x"}'),
        ],
        meta={"thinking_blocks": [{"type": "thinking", "thinking": "pondering", "signature": "sig"}]},
    )
    exchange = [
        assistant,
        _tool_result(MANAGE_CONTEXT_TOOL_NAME, "c1", "scheduled"),
    ]
    cleaned = strip_manage_context_from_exchange(exchange)
    assert cleaned == []


@pytest.mark.asyncio
async def test_apply_solo_manage_context_does_not_leave_thinking_only_message():
    from beeai_framework.backend.message import MessageReasoningContent

    task = _task_message()
    old = _exchange("shell", "old", "noise")
    solo = [
        AssistantMessage(
            [
                MessageReasoningContent(text="compact now"),
                _tool_call(MANAGE_CONTEXT_TOOL_NAME, "c1"),
            ],
            meta={"thinking_blocks": [{"type": "thinking", "thinking": "compact now", "signature": "sig"}]},
        ),
        _tool_result(MANAGE_CONTEXT_TOOL_NAME, "c1", "scheduled"),
    ]
    state = _state_with_messages([task, *old, *solo])
    state.pending_context_compaction = ManageContextSchema(
        durable_summary="paths and TF id",
        keep_recent_exchanges=1,
    )
    await apply_pending_context_compaction(state)

    assert not any(isinstance(m, AssistantMessage) for m in state.memory.messages)
    assert any(m.meta.get(YMIR_CONTEXT_SUMMARY_META_KEY) for m in state.memory.messages)
    assert state.memory.messages[0].meta.get(YMIR_PROTECTED_META_KEY) is True


@pytest.mark.asyncio
async def test_manage_context_tool_only_schedules():
    state = _state_with_messages([_task_message()])
    before = list(state.memory.messages)
    tool = ManageContextTool(state=state)
    output = await tool._run(
        ManageContextSchema(durable_summary="important fact", keep_recent_exchanges=1),
        None,
        MagicMock(),
    )
    assert "scheduled" in output.get_text_content().lower()
    assert state.pending_context_compaction is not None
    assert state.pending_context_compaction.durable_summary == "important fact"
    assert list(state.memory.messages) == before


@pytest.mark.asyncio
async def test_apply_compaction_protects_later_task_messages():
    task1 = _task_message("Your task: first turn")
    ex1 = _exchange("view", "c1", "dead end dump " * 20)
    task2 = _task_message("Your task: second turn")
    ex2 = _exchange("shell", "c2", "also failed")
    state = _state_with_messages([task1, *ex1, task2, *ex2])
    state.pending_context_compaction = ManageContextSchema(
        durable_summary="CVE-2026-1; approach A failed",
        keep_recent_exchanges=1,
    )

    applied = await apply_pending_context_compaction(state)
    assert applied is True

    protected = [m for m in state.memory.messages if m.meta.get(YMIR_PROTECTED_META_KEY)]
    assert len(protected) == 2
    assert "first turn" in protected[0].text
    assert "second turn" in protected[1].text


@pytest.mark.asyncio
async def test_apply_compaction_protects_task_and_keeps_recent():
    task = _task_message()
    ex1 = _exchange("view", "c1", "dead end dump " * 20)
    ex2 = _exchange("shell", "c2", "also failed")
    ex3 = [
        AssistantMessage(
            [
                _tool_call("view", "c3"),
                _tool_call(MANAGE_CONTEXT_TOOL_NAME, "c4"),
            ]
        ),
        _tool_result("view", "c3", "useful current file"),
        _tool_result(MANAGE_CONTEXT_TOOL_NAME, "c4", "scheduled"),
    ]
    state = _state_with_messages([task, *ex1, *ex2, *ex3])
    state.pending_context_compaction = ManageContextSchema(
        durable_summary="CVE-2026-1; TF req tf-9; approach A failed",
        keep_recent_exchanges=1,
    )

    applied = await apply_pending_context_compaction(state)
    assert applied is True
    assert state.pending_context_compaction is None

    messages = state.memory.messages
    assert messages[0] is task or (
        messages[0].meta.get(YMIR_PROTECTED_META_KEY) and "write a reproducer" in messages[0].text
    )
    assert messages[0].meta.get(YMIR_PROTECTED_META_KEY) is True

    summaries = [m for m in messages if m.meta.get(YMIR_CONTEXT_SUMMARY_META_KEY)]
    assert len(summaries) == 1
    assert "CVE-2026-1" in summaries[0].text
    assert "dead end dump" not in "".join(m.text for m in messages if hasattr(m, "text"))
    assert "useful current file" in "".join(
        getattr(c, "result", "") for m in messages if isinstance(m, ToolMessage) for c in m.content
    )
    # manage_context stripped from kept exchange
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            assert all(c.tool_name != MANAGE_CONTEXT_TOOL_NAME for c in msg.get_tool_calls())
        if isinstance(msg, ToolMessage):
            assert all(getattr(c, "tool_name", None) != MANAGE_CONTEXT_TOOL_NAME for c in msg.content)


@pytest.mark.asyncio
async def test_apply_preserves_tool_call_result_pairing():
    task = _task_message()
    old = _exchange("shell", "old", "noise")
    kept = [
        AssistantMessage([_tool_call("view", "a"), _tool_call("shell", "b")]),
        _tool_result("view", "a", "file"),
        _tool_result("shell", "b", "out"),
    ]
    state = _state_with_messages([task, *old, *kept])
    state.pending_context_compaction = ManageContextSchema(
        durable_summary="keep going",
        keep_recent_exchanges=1,
    )
    await apply_pending_context_compaction(state)

    assistant_msgs = [m for m in state.memory.messages if isinstance(m, AssistantMessage)]
    tool_msgs = [m for m in state.memory.messages if isinstance(m, ToolMessage)]
    assert len(assistant_msgs) == 1
    call_ids = {c.id for c in assistant_msgs[0].get_tool_calls()}
    result_ids = {c.tool_call_id for m in tool_msgs for c in m.content}
    assert call_ids == result_ids == {"a", "b"}


@pytest.mark.asyncio
async def test_apply_with_nothing_to_drop_still_strips_manage_context():
    task = _task_message()
    only = [
        AssistantMessage(
            [
                _tool_call("view", "c1"),
                _tool_call(MANAGE_CONTEXT_TOOL_NAME, "c2"),
            ]
        ),
        _tool_result("view", "c1", "data"),
        _tool_result(MANAGE_CONTEXT_TOOL_NAME, "c2", "scheduled"),
    ]
    state = _state_with_messages([task, *only])
    state.pending_context_compaction = ManageContextSchema(
        durable_summary="noop summary",
        keep_recent_exchanges=1,
    )
    await apply_pending_context_compaction(state)
    assert not any(m.meta.get(YMIR_CONTEXT_SUMMARY_META_KEY) for m in state.memory.messages)
    assert any(isinstance(m, ToolMessage) and m.content[0].result == "data" for m in state.memory.messages)


@pytest.mark.asyncio
async def test_apply_noop_without_pending():
    state = _state_with_messages([_task_message()])
    assert await apply_pending_context_compaction(state) is False
