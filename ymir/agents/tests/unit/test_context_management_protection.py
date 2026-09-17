"""Tests for deterministic preservation during protected context compaction."""

from copy import deepcopy

import pytest
from beeai_framework.backend import (
    AssistantMessage,
    MessageToolCallContent,
    MessageToolResultContent,
    ToolMessage,
    UserMessage,
)
from beeai_framework.backend.message import MessageTextContent
from beeai_framework.memory import UnconstrainedMemory
from flexmock import flexmock

from ymir.agents.reasoning_agent.agent import ReasoningAgent
from ymir.agents.reasoning_agent.context_management import (
    MANAGE_CONTEXT_TOOL_NAME,
    YMIR_CONTEXT_SUMMARY_META_KEY,
    YMIR_PROTECTED_META_KEY,
    YMIR_ROLE_META_KEY,
    ManageContextSchema,
    apply_pending_context_compaction,
)
from ymir.agents.reasoning_agent.types import ReasoningAgentRunState

RULE_TOOLS = ("get_shared_rules", "get_maintainer_rules")


def _call(name: str, call_id: str, args: str = "{}") -> MessageToolCallContent:
    return MessageToolCallContent(type="tool-call", id=call_id, tool_name=name, args=args)


def _result(name: str, call_id: str, value: str) -> MessageToolResultContent:
    return MessageToolResultContent(tool_name=name, tool_call_id=call_id, result=value)


def _exchange(name: str, call_id: str, value: str, args: str = "{}") -> list:
    return [
        AssistantMessage(_call(name, call_id, args)),
        ToolMessage(_result(name, call_id, value)),
    ]


def _task(text: str) -> UserMessage:
    return UserMessage(
        text,
        meta={YMIR_PROTECTED_META_KEY: True, YMIR_ROLE_META_KEY: "task"},
    )


def _state(messages: list) -> ReasoningAgentRunState:
    memory = UnconstrainedMemory()
    memory.messages.extend(messages)
    return ReasoningAgentRunState(answer=None, result=None, memory=memory, steps=[], iteration=1)


def _schedule(state: ReasoningAgentRunState, summary: str = "continue", keep: int = 1) -> None:
    state.pending_context_compaction = ManageContextSchema(
        durable_summary=summary,
        keep_recent_exchanges=keep,
    )


def _plain(messages: list) -> list[dict]:
    return [message.to_plain() for message in messages]


def _tool_calls(messages: list, name: str | None = None) -> list[MessageToolCallContent]:
    calls = [
        call
        for message in messages
        if isinstance(message, AssistantMessage)
        for call in message.get_tool_calls()
    ]
    return calls if name is None else [call for call in calls if call.tool_name == name]


def _tool_results(messages: list, name: str | None = None) -> list[MessageToolResultContent]:
    results = [
        result
        for message in messages
        if isinstance(message, ToolMessage)
        for result in message.content
        if isinstance(result, MessageToolResultContent)
    ]
    return results if name is None else [result for result in results if result.tool_name == name]


@pytest.mark.asyncio
async def test_rules_survive_exactly_when_summary_omits_them():
    rules = "  Never change Release.\nUnicode: žluťoučký 🐍\n\n"
    args = '{"package":"curl","file_path":"AGENTS.md"}'
    state = _state(
        [
            _task("Backport RHEL-1"),
            *_exchange("get_maintainer_rules", "rules", rules, args),
            *_exchange("view", "obsolete", "large dump " * 100),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state, "Investigation finished; continue with the patch.")

    assert await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

    assert _tool_calls(state.memory.messages, "get_maintainer_rules")[0].args == args
    assert _tool_results(state.memory.messages, "get_maintainer_rules")[0].result == rules
    assert "large dump" not in str(_plain(state.memory.messages))


@pytest.mark.asyncio
async def test_invented_permission_cannot_replace_contrary_rules():
    state = _state(
        [
            _task("Backport RHEL-2"),
            *_exchange(
                "get_maintainer_rules",
                "rules",
                "Spec edits are forbidden.",
                '{"package":"x"}',
            ),
            *_exchange("shell", "old", "failed"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state, "Maintainer allows every spec edit.")

    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

    assert _tool_results(state.memory.messages, "get_maintainer_rules")[0].result == (
        "Spec edits are forbidden."
    )
    assert any("Maintainer allows" in message.text for message in state.memory.messages)


@pytest.mark.asyncio
async def test_shared_discovery_and_all_rule_files_remain_distinct_and_ordered():
    exchanges = [
        _exchange("get_shared_rules", "shared", '["crypto", "rpm"]', '{"package":"openssl"}'),
        _exchange(
            "get_maintainer_rules",
            "crypto",
            "crypto rules",
            '{"package":"shared-rules","file_path":"crypto/AGENTS.md"}',
        ),
        _exchange(
            "get_maintainer_rules",
            "rpm",
            "rpm rules",
            '{"package":"shared-rules","file_path":"rpm/AGENTS.md"}',
        ),
        _exchange("get_maintainer_rules", "pkg", "package rules", '{"package":"openssl"}'),
    ]
    state = _state(
        [
            _task("Backport RHEL-3"),
            *(message for exchange in exchanges for message in exchange),
            *_exchange("view", "old", "obsolete"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state)

    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

    protected_call_ids = [
        call.id for call in _tool_calls(state.memory.messages) if call.tool_name in RULE_TOOLS
    ]
    assert protected_call_ids == [
        "shared",
        "crypto",
        "rpm",
        "pkg",
    ]
    protected_results = [
        result.result for result in _tool_results(state.memory.messages) if result.tool_name in RULE_TOOLS
    ]
    assert protected_results == [
        '["crypto", "rpm"]',
        "crypto rules",
        "rpm rules",
        "package rules",
    ]


@pytest.mark.asyncio
async def test_only_structured_rule_calls_pin_an_exchange():
    mention = _exchange("view", "mention", "text says get_maintainer_rules")
    real = _exchange("get_maintainer_rules", "real", "rules", '{"package":"pkg"}')
    state = _state(
        [
            _task("Backport RHEL-4"),
            *mention,
            *real,
            *_exchange("shell", "old", "obsolete"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state)

    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

    assert not _tool_calls(state.memory.messages, "view")
    assert [call.id for call in _tool_calls(state.memory.messages, "get_maintainer_rules")] == ["real"]


@pytest.mark.parametrize(
    "value",
    ["[]", "No maintainer rules found.", "HTTP 503: unavailable", "ToolError: request failed"],
)
@pytest.mark.asyncio
async def test_failed_and_empty_rule_results_are_preserved(value):
    state = _state(
        [
            _task("Backport RHEL-5"),
            *_exchange("get_maintainer_rules", "rules", value),
            *_exchange("view", "old", "obsolete"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state)
    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    assert _tool_results(state.memory.messages, "get_maintainer_rules")[0].result == value


@pytest.mark.asyncio
async def test_repeated_fetches_are_not_deduplicated_or_reordered():
    state = _state(
        [
            _task("Backport RHEL-6"),
            *_exchange("get_maintainer_rules", "v1", "version one"),
            *_exchange("get_maintainer_rules", "v2", "version two"),
            *_exchange("get_maintainer_rules", "err", "HTTP 500"),
            *_exchange("view", "old", "obsolete"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state)
    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    assert [result.result for result in _tool_results(state.memory.messages, "get_maintainer_rules")] == [
        "version one",
        "version two",
        "HTTP 500",
    ]


@pytest.mark.asyncio
async def test_mixed_batch_keeps_all_non_compaction_siblings_and_metadata():
    assistant = AssistantMessage(
        [
            MessageTextContent(text="useful text"),
            _call("get_maintainer_rules", "rules", '{"package":"pkg"}'),
            _call("view", "view"),
            _call(MANAGE_CONTEXT_TOOL_NAME, "compact"),
        ],
        meta={"thinking_blocks": [{"type": "thinking", "thinking": "x", "signature": "sig"}]},
    )
    batched = ToolMessage(
        [
            _result("get_maintainer_rules", "rules", "exact rules"),
            _result("view", "view", "useful file"),
            _result(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ],
        meta={"custom": "result metadata"},
    )
    state = _state(
        [
            _task("Backport RHEL-7"),
            *_exchange("shell", "old", "obsolete"),
            assistant,
            batched,
        ]
    )
    original = deepcopy(_plain(state.memory.messages))
    _schedule(state)

    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

    kept_assistant = next(
        message for message in state.memory.messages if isinstance(message, AssistantMessage)
    )
    kept_result = next(message for message in state.memory.messages if isinstance(message, ToolMessage))
    assert [call.tool_name for call in kept_assistant.get_tool_calls()] == [
        "get_maintainer_rules",
        "view",
    ]
    assert kept_assistant.meta["thinking_blocks"][0]["signature"] == "sig"
    assert [result.result for result in kept_result.content] == ["exact rules", "useful file"]
    assert kept_result.meta["custom"] == "result metadata"
    assert _plain([assistant, batched]) == original[-2:]


@pytest.mark.parametrize("keep", [1, 2, 20])
@pytest.mark.asyncio
async def test_pinned_and_recent_exchanges_form_ordered_union_without_duplicates(keep):
    state = _state(
        [
            _task("Backport RHEL-8"),
            *_exchange("get_maintainer_rules", "old-rules", "rules"),
            *_exchange("view", "middle", "middle"),
            *_exchange("get_shared_rules", "recent-rules", "shared"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state, keep=keep)
    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    ids = [call.id for call in _tool_calls(state.memory.messages)]
    assert ids.count("old-rules") == 1
    assert ids.count("recent-rules") == 1
    assert ids.index("old-rules") < ids.index("recent-rules")
    assert ("middle" in ids) == (keep >= 3)


@pytest.mark.asyncio
async def test_retry_tasks_and_rules_keep_original_chronology():
    first = _task("Initial checkout")
    second = _task("Checkout reset; recheck current files")
    state = _state(
        [
            first,
            *_exchange("get_maintainer_rules", "old-rules", "old"),
            *_exchange("view", "old-work", "obsolete"),
            second,
            *_exchange("get_maintainer_rules", "new-rules", "new"),
            *_exchange("shell", "current", "current state"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state, keep=2)
    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

    messages = state.memory.messages
    positions = {
        "task1": next(i for i, message in enumerate(messages) if "Initial" in message.text),
        "old": next(
            i
            for i, message in enumerate(messages)
            if isinstance(message, AssistantMessage)
            and any(call.id == "old-rules" for call in message.get_tool_calls())
        ),
        "task2": next(i for i, message in enumerate(messages) if "Checkout reset" in message.text),
        "new": next(
            i
            for i, message in enumerate(messages)
            if isinstance(message, AssistantMessage)
            and any(call.id == "new-rules" for call in message.get_tool_calls())
        ),
        "current": next(
            i
            for i, message in enumerate(messages)
            if isinstance(message, AssistantMessage)
            and any(call.id == "current" for call in message.get_tool_calls())
        ),
    }
    assert list(positions.values()) == sorted(positions.values())


@pytest.mark.asyncio
async def test_multi_message_user_input_survives_repeated_compaction():
    agent = ReasoningAgent(llm=flexmock(allow_parallel_tool_calls=False))
    inputs = agent._process_input(
        [
            UserMessage(
                "Do not change BuildRequires.",
                meta={"source": "user instruction"},
                id="earlier-instruction",
            ),
            UserMessage("Backport RHEL-42."),
        ],
        backstory=None,
    )
    assert not inputs[0].meta.get(YMIR_PROTECTED_META_KEY)
    expected = deepcopy(inputs)
    state = _state(inputs)

    for attempt in range(2):
        state.memory.messages.extend(_exchange("view", f"work-{attempt}", "obsolete output"))
        state.memory.messages.extend(_exchange(MANAGE_CONTEXT_TOOL_NAME, f"compact-{attempt}", "scheduled"))
        _schedule(state, summary=f"Cumulative progress {attempt}.")

        assert await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

        retained_inputs = [
            message
            for message in state.memory.messages
            if isinstance(message, UserMessage) and not message.meta.get(YMIR_CONTEXT_SUMMARY_META_KEY)
        ]
        assert _plain(retained_inputs) == _plain(expected)
        assert [message.meta for message in retained_inputs] == [message.meta for message in expected]
        assert [message.id for message in retained_inputs] == [message.id for message in expected]
        summaries = [
            message for message in state.memory.messages if message.meta.get(YMIR_CONTEXT_SUMMARY_META_KEY)
        ]
        assert len(summaries) == 1
        assert f"Cumulative progress {attempt}." in summaries[0].text
        assert "obsolete output" not in str(_plain(state.memory.messages))


@pytest.mark.asyncio
async def test_no_removable_history_avoids_unnecessary_summary_and_cleans_compaction():
    state = _state(
        [
            _task("Backport RHEL-9"),
            *_exchange("get_maintainer_rules", "rules", "rules"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state, summary="unneeded")
    await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    assert not any(message.meta.get(YMIR_CONTEXT_SUMMARY_META_KEY) for message in state.memory.messages)
    assert not _tool_calls(state.memory.messages, MANAGE_CONTEXT_TOOL_NAME)


@pytest.mark.asyncio
async def test_three_compactions_replace_summaries_without_duplicating_rules():
    state = _state(
        [
            _task("Backport RHEL-10"),
            *_exchange("get_maintainer_rules", "rules", "rules"),
        ]
    )
    for number in range(3):
        state.memory.messages.extend(_exchange("view", f"work-{number}", f"obsolete-{number}"))
        state.memory.messages.extend(_exchange(MANAGE_CONTEXT_TOOL_NAME, f"compact-{number}", "scheduled"))
        _schedule(state, summary=f"cumulative summary {number}")
        await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

    assert [call.id for call in _tool_calls(state.memory.messages, "get_maintainer_rules")] == ["rules"]
    summaries = [
        message for message in state.memory.messages if message.meta.get(YMIR_CONTEXT_SUMMARY_META_KEY)
    ]
    assert len(summaries) == 1
    assert "cumulative summary 2" in summaries[0].text


@pytest.mark.parametrize(
    "messages",
    [
        [AssistantMessage(_call("view", "missing"))],
        [ToolMessage(_result("view", "orphan", "x"))],
        [
            AssistantMessage(_call("view", "dup")),
            ToolMessage([_result("view", "dup", "x"), _result("view", "dup", "y")]),
        ],
    ],
)
@pytest.mark.asyncio
async def test_malformed_exchange_skips_transactionally(messages):
    state = _state([_task("Backport RHEL-11"), *messages])
    before = deepcopy(_plain(state.memory.messages))
    _schedule(state)
    assert not await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    assert _plain(state.memory.messages) == before
    assert state.pending_context_compaction is None


@pytest.mark.asyncio
async def test_blank_summary_and_multiple_compaction_calls_are_rejected():
    multiple = [
        AssistantMessage(
            [
                _call(MANAGE_CONTEXT_TOOL_NAME, "one"),
                _call(MANAGE_CONTEXT_TOOL_NAME, "two"),
            ]
        ),
        ToolMessage(
            [
                _result(MANAGE_CONTEXT_TOOL_NAME, "one", "scheduled"),
                _result(MANAGE_CONTEXT_TOOL_NAME, "two", "scheduled"),
            ]
        ),
    ]
    cases = [
        ("   ", _exchange(MANAGE_CONTEXT_TOOL_NAME, "one", "scheduled")),
        ("x", multiple),
    ]
    for summary, tail in cases:
        state = _state([_task("Backport RHEL-12"), *_exchange("view", "old", "old"), *tail])
        before = deepcopy(_plain(state.memory.messages))
        _schedule(state, summary=summary)
        assert not await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
        assert _plain(state.memory.messages) == before
        assert state.pending_context_compaction is None


@pytest.mark.asyncio
async def test_standalone_compaction_rolls_back_if_cleaned_history_ends_with_assistant_text():
    state = _state(
        [
            _task("Backport RHEL-14"),
            AssistantMessage("Previous assistant text"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    before = deepcopy(_plain(state.memory.messages))
    _schedule(state, keep=2)

    assert not await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    assert _plain(state.memory.messages) == before
    assert isinstance(state.memory.messages[-1], ToolMessage)


@pytest.mark.asyncio
async def test_rejected_duplicate_does_not_block_a_later_valid_compaction():
    duplicate = [
        AssistantMessage(
            [
                _call(MANAGE_CONTEXT_TOOL_NAME, "duplicate-one"),
                _call(MANAGE_CONTEXT_TOOL_NAME, "duplicate-two"),
            ]
        ),
        ToolMessage(
            [
                _result(MANAGE_CONTEXT_TOOL_NAME, "duplicate-one", "scheduled"),
                _result(MANAGE_CONTEXT_TOOL_NAME, "duplicate-two", "scheduled"),
            ]
        ),
    ]
    state = _state([_task("Backport RHEL-15"), *_exchange("view", "old", "obsolete"), *duplicate])
    _schedule(state)
    assert not await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)

    state.memory.messages.extend(_exchange(MANAGE_CONTEXT_TOOL_NAME, "valid", "scheduled"))
    _schedule(state, summary="valid retry")

    assert await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    assert not _tool_calls(state.memory.messages, MANAGE_CONTEXT_TOOL_NAME)
    assert any(
        message.meta.get(YMIR_CONTEXT_SUMMARY_META_KEY) and "valid retry" in message.text
        for message in state.memory.messages
    )


@pytest.mark.asyncio
async def test_candidate_population_failure_leaves_original_memory_untouched(caplog):
    state = _state(
        [
            _task("Backport RHEL-13"),
            *_exchange("get_maintainer_rules", "rules", "secret rule text"),
            *_exchange("view", "old", "obsolete"),
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    original_memory = state.memory
    before = deepcopy(_plain(state.memory.messages))
    _schedule(state)

    async def fail_population(*_args, **_kwargs):
        raise RuntimeError("candidate population failed: secret rule text")

    flexmock(UnconstrainedMemory).should_receive("add_many").replace_with(fail_population).once()

    assert not await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    assert state.memory is original_memory
    assert _plain(state.memory.messages) == before
    assert state.pending_context_compaction is None
    assert "RuntimeError" in caplog.text
    assert "fail_population" in caplog.text
    assert "secret rule text" not in caplog.text


@pytest.mark.parametrize(
    ("summary", "messages", "reason"),
    [
        (" ", [], "blank durable summary"),
        ("secret summary", [AssistantMessage(_call("view", "missing"))], "malformed tool exchange"),
    ],
)
@pytest.mark.asyncio
async def test_validation_logs_safe_reason_without_context(caplog, summary, messages, reason):
    state = _state([_task("secret task"), *messages])
    _schedule(state, summary=summary)
    original_memory = state.memory
    assert not await apply_pending_context_compaction(state, protected_tool_names=RULE_TOOLS)
    assert state.memory is original_memory
    assert reason in caplog.text
    assert "secret task" not in caplog.text
    assert "secret summary" not in caplog.text


@pytest.mark.asyncio
async def test_default_empty_policy_retains_legacy_reordering_behavior():
    task1 = _task("first")
    task2 = _task("second")
    state = _state(
        [
            task1,
            *_exchange("view", "old", "old"),
            task2,
            *_exchange(MANAGE_CONTEXT_TOOL_NAME, "compact", "scheduled"),
        ]
    )
    _schedule(state)
    await apply_pending_context_compaction(state, protected_tool_names=())
    assert state.memory.messages[:2] == [task1, task2]
