"""The final_answer validator runs inside the agent loop so the model can fix a rejected answer."""

import json

import pytest
from beeai_framework.agents import AgentExecutionConfig
from beeai_framework.agents.tool_calling.utils import ToolCallChecker, ToolCallCheckerConfig
from beeai_framework.backend import AssistantMessage, ChatModelOutput, MessageToolCallContent, ToolMessage
from beeai_framework.emitter import Emitter
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.tools import ToolError
from flexmock import flexmock
from pydantic import BaseModel

from ymir.agents.reasoning_agent._runner import FinalAnswerTool, ReasoningAgentRunner
from ymir.agents.reasoning_agent.types import (
    ReasoningAgentRunState,
    ReasoningAgentTemplates,
    RequirementEvaluation,
)


def _require_key(text: str) -> str:
    payload = json.loads(text)
    if "key" not in payload:
        raise ValueError("`key` is missing")
    payload["checked"] = True
    return json.dumps(payload)


def _state() -> ReasoningAgentRunState:
    return ReasoningAgentRunState(
        answer=None, result=None, memory=UnconstrainedMemory(), steps=[], iteration=0
    )


def _runner(
    validate_output=None, *, unconstrained: bool = False, force_final_answer_as_tool: bool = True
) -> ReasoningAgentRunner:
    return ReasoningAgentRunner(
        config=AgentExecutionConfig(max_retries_per_step=3, total_max_retries=20),
        tool_call_cycle_checker=ToolCallChecker(ToolCallCheckerConfig()),
        force_final_answer_as_tool=force_final_answer_as_tool,
        expected_output="Answer with a JSON object that has a `key` field.",
        run_context=flexmock(emitter=Emitter.root().child(namespace=["test"])),
        tools=[],
        templates=ReasoningAgentTemplates(),
        llm=flexmock(allow_parallel_tool_calls=False, provider_id="vertexai"),
        unconstrained=unconstrained,
        validate_output=validate_output,
    )


async def _run_turn(runner: ReasoningAgentRunner) -> None:
    method = runner._run_unconstrained if runner._unconstrained else runner._run_constrained
    await method(_evaluation(runner))


def _final_answer_response(answer: dict, call_id: str) -> ChatModelOutput:
    call = MessageToolCallContent(
        type="tool-call",
        id=call_id,
        tool_name="final_answer",
        args=json.dumps({"response": json.dumps(answer)}),
    )
    return ChatModelOutput(output=[AssistantMessage(call)])


def _evaluation(runner: ReasoningAgentRunner) -> RequirementEvaluation:
    return RequirementEvaluation(
        allowed_tools=list(runner._all_tools),
        hidden_tools=[],
        can_stop=True,
        tool_choice="required",
        all_tools=list(runner._all_tools),
    )


@pytest.mark.asyncio
async def test_final_answer_without_validator_is_accepted_as_is():
    state = _state()
    tool = FinalAnswerTool("instructions", state=state)

    await tool.run({"response": '{"other": 1}'})

    assert state.answer.text == '{"other": 1}'


@pytest.mark.asyncio
async def test_final_answer_stores_validator_output():
    state = _state()
    tool = FinalAnswerTool("instructions", state=state, validate_output=_require_key)

    await tool.run({"response": '{"key": "value"}'})

    assert json.loads(state.answer.text) == {"key": "value", "checked": True}


@pytest.mark.asyncio
async def test_final_answer_structured_result_matches_validator_output():
    state = _state()
    tool = FinalAnswerTool("instructions", state=state, validate_output=_require_key)

    await tool.run({"response": '{"key": "value"}'})

    assert state.result.response == state.answer.text


class _Answer(BaseModel):
    key: str
    checked: bool = False


@pytest.mark.asyncio
async def test_final_answer_structured_result_matches_validator_output_for_schema():
    state = _state()
    tool = FinalAnswerTool(_Answer, state=state, validate_output=_require_key)

    await tool.run({"key": "value"})

    assert state.result == _Answer(key="value", checked=True)
    assert _Answer.model_validate_json(state.answer.text) == state.result


@pytest.mark.asyncio
async def test_rejected_final_answer_raises_tool_error_and_keeps_run_going():
    state = _state()
    tool = FinalAnswerTool("instructions", state=state, validate_output=_require_key)

    with pytest.raises(ToolError) as excinfo:
        await tool.run({"response": '{"other": 1}'})

    assert "The final answer was rejected: `key` is missing" in excinfo.value.message
    assert "call 'final_answer' again" in excinfo.value.message
    assert state.answer is None
    assert state.result is None


@pytest.mark.asyncio
async def test_clone_keeps_validator():
    tool = FinalAnswerTool("instructions", state=_state(), validate_output=_require_key)

    cloned = await tool.clone()

    with pytest.raises(ToolError):
        await cloned.run({"response": '{"other": 1}'})


@pytest.mark.asyncio
@pytest.mark.parametrize("unconstrained", [False, True])
async def test_runner_returns_rejection_to_model_and_accepts_corrected_answer(unconstrained):
    runner = _runner(validate_output=_require_key, unconstrained=unconstrained)
    responses = iter(
        [
            _final_answer_response({"other": 1}, "call_bad"),
            _final_answer_response({"key": "value"}, "call_good"),
        ]
    )

    async def _next_response(_evaluation):
        return next(responses)

    flexmock(runner).should_receive("_run_llm").replace_with(_next_response).times(2)

    await _run_turn(runner)

    assert runner._state.answer is None
    rejection = runner._state.memory.messages[-1]
    assert isinstance(rejection, ToolMessage)
    assert "The final answer was rejected: `key` is missing" in rejection.content[0].result

    await _run_turn(runner)

    assert json.loads(runner._state.answer.text) == {"key": "value", "checked": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("unconstrained", [False, True])
async def test_runner_gives_up_after_retry_limit(unconstrained):
    runner = _runner(validate_output=_require_key, unconstrained=unconstrained)
    attempts = iter(range(100))

    async def _bad_response(_evaluation):
        attempt = next(attempts)
        return _final_answer_response({"other": attempt}, f"call_{attempt}")

    flexmock(runner).should_receive("_run_llm").replace_with(_bad_response)

    with pytest.raises(FrameworkError, match=r"retries .* has been reached") as excinfo:
        for _ in range(10):
            await _run_turn(runner)

    assert "`key` is missing" in excinfo.value.explain()

    # max_retries_per_step=3 allows three rejected answers; the fourth ends the run.
    assert next(attempts) == 4
    assert runner._state.answer is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("validate_output", "accepted"), [(None, True), (_require_key, False)])
async def test_runner_accepts_plain_text_only_without_validator(validate_output, accepted):
    runner = _runner(validate_output=validate_output, unconstrained=True, force_final_answer_as_tool=False)

    async def _text_response(_evaluation):
        # Empty text is the case _create_final_answer_tool_call cannot turn into a tool call.
        return ChatModelOutput(output=[AssistantMessage("")])

    flexmock(runner).should_receive("_run_llm").replace_with(_text_response)

    await _run_turn(runner)

    if accepted:
        assert runner._state.answer is not None
    else:
        assert runner._state.answer is None
        assert "final_answer" in runner._state.memory.messages[-1].text
