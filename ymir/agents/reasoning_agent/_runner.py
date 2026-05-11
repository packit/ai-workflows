import json
import uuid
from collections.abc import Sequence
from typing import Any, Self

from beeai_framework.agents import AgentError, AgentExecutionConfig
from beeai_framework.agents._utils import run_tools
from beeai_framework.agents.requirement.agent import RequirementAgentRequirement
from beeai_framework.agents.requirement.requirements.events import (
    RequirementInitEvent,
    requirement_event_types,
)
from beeai_framework.agents.requirement.requirements.requirement import Rule
from beeai_framework.agents.tool_calling.utils import ToolCallChecker
from beeai_framework.backend import (
    AnyMessage,
    AssistantMessage,
    ChatModel,
    ChatModelOutput,
    MessageToolCallContent,
    MessageToolResultContent,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from beeai_framework.backend.chat import ChatModelOptions
from beeai_framework.backend.errors import ChatModelToolCallError
from beeai_framework.backend.utils import parse_broken_json
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.memory.utils import TEMP_MESSAGE_META_KEY, delete_messages_by_meta_key
from beeai_framework.middleware.stream_tool_call import StreamToolCallMiddleware
from beeai_framework.tools import AnyTool, StringToolOutput, Tool, ToolRunOptions
from beeai_framework.utils.counter import RetryCounter
from beeai_framework.utils.lists import ensure_strictly_increasing, find_last_index
from beeai_framework.utils.strings import find_first_pair, generate_random_string, to_json, to_safe_word
from pydantic import BaseModel, Field

from ymir.agents.reasoning_agent.events import (
    ReasoningAgentFinalAnswerEvent,
    ReasoningAgentStartEvent,
    ReasoningAgentSuccessEvent,
)
from ymir.agents.reasoning_agent.prompts import (
    ReasoningAgentToolErrorPromptInput,
    ReasoningAgentToolTemplateDefinition,
)
from ymir.agents.reasoning_agent.types import (
    ReasoningAgentRunState,
    ReasoningAgentRunStateStep,
    ReasoningAgentTemplates,
    RequirementEvaluation,
)


class FinalAnswerToolSchema(BaseModel):
    response: str = Field(description="The final answer to the user")


class FinalAnswerTool(Tool[BaseModel, ToolRunOptions, StringToolOutput]):
    name = "final_answer"
    description = "Sends the final answer to the user"

    def __init__(self, expected_output: str | type[BaseModel] | None, state: ReasoningAgentRunState) -> None:
        super().__init__()
        self._expected_output = expected_output
        self._state = state
        self.instructions = expected_output if isinstance(expected_output, str) else None
        self.custom_schema = isinstance(expected_output, type)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "final_answer"], creator=self)

    @property
    def input_schema(self) -> type[BaseModel]:
        expected_output = self._expected_output

        if expected_output is None:
            return FinalAnswerToolSchema
        if isinstance(expected_output, type) and issubclass(expected_output, BaseModel):
            return expected_output
        if isinstance(expected_output, str):

            class CustomFinalAnswerToolSchema(FinalAnswerToolSchema):
                response: str = Field(description=expected_output)  # type: ignore

            return CustomFinalAnswerToolSchema
        return FinalAnswerToolSchema

    async def _run(
        self, input: BaseModel, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        self._state.result = input
        if self.input_schema is self._expected_output:
            self._state.answer = AssistantMessage(input.model_dump_json())
        else:
            self._state.answer = AssistantMessage(input.response)  # type: ignore

        return StringToolOutput("Message has been sent")

    async def clone(self) -> Self:
        tool = self.__class__(expected_output=self._expected_output, state=self._state.model_copy())
        tool.name = self.name
        tool.description = self.description
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool


class ReasoningAgentRunner:
    def __init__(
        self,
        *,
        config: AgentExecutionConfig,
        tool_call_cycle_checker: ToolCallChecker,
        force_final_answer_as_tool: bool,
        expected_output: Any,
        run_context: RunContext,
        tools: list[AnyTool],
        templates: ReasoningAgentTemplates,
        llm: ChatModel,
        requirements: Sequence[RequirementAgentRequirement] | None = None,
        unconstrained: bool = False,
    ) -> None:
        self._ctx = run_context
        self._llm = llm
        self._templates = templates
        self._force_final_answer_as_tool = force_final_answer_as_tool
        self._state = ReasoningAgentRunState(
            answer=None, result=None, memory=UnconstrainedMemory(), steps=[], iteration=0
        )
        self._final_answer = FinalAnswerTool(expected_output, state=self._state)
        self._tools = tools
        self._all_tools: list[AnyTool] = [*tools, self._final_answer]
        self._run_config = config
        self._tool_call_cycle_checker = tool_call_cycle_checker
        self._requirements: list[RequirementAgentRequirement] = list(requirements or [])
        self._unconstrained = unconstrained

        max_retries_per_iteration = 0 if config.max_retries_per_step is None else config.max_retries_per_step
        self._iteration_error_counter = RetryCounter(
            error_type=AgentError, max_retries=max_retries_per_iteration
        )

        max_retries = 0 if config.total_max_retries is None else config.total_max_retries
        max_retries = max(max_retries_per_iteration, max_retries)
        self._global_error_counter = RetryCounter(error_type=AgentError, max_retries=max_retries)

    async def _init_requirements(self) -> None:
        for requirement in self._requirements:
            emitter = self._ctx.emitter.child(
                group_id=to_safe_word(requirement.name),
                creator=requirement,
                events=requirement_event_types,
            )
            emitter.namespace.append("requirement")
            tools = list(self._all_tools)
            await emitter.emit("init", RequirementInitEvent(tools=tools))
            await requirement.init(tools=tools, ctx=self._ctx)

    async def _evaluate_requirements(self, extra_rules: list[Rule] | None = None) -> RequirementEvaluation:
        rules_by_tool: dict[str, list[tuple[int, Rule]]] = {t.name: [] for t in self._all_tools}

        for requirement in self._requirements:
            if not requirement.enabled:
                continue
            generated_rules = await requirement.run(self._state)  # type: ignore[arg-type]
            for rule in generated_rules:
                if rule.target not in rules_by_tool:
                    raise ValueError(
                        f"Tool '{rule.target}' not found in ({','.join(t.name for t in self._all_tools)})."
                    )
                rules_by_tool[rule.target].append((requirement.priority, rule))

        for rule in extra_rules or []:
            if rule.target not in rules_by_tool:
                raise ValueError(f"Tool '{rule.target}' not found.")
            entries = rules_by_tool[rule.target]
            priority = max(e[0] for e in entries) + 1 if entries else 1
            entries.append((priority, rule))

        allowed: list[AnyTool] = []
        hidden: list[AnyTool] = []
        forced: AnyTool | None = None
        forced_priority = 0
        prevent_stop = False
        prevent_step_refs: list[dict[str, Any]] = []
        reason_by_tool: dict[str, str | None] = {}
        reasons: list[str] = []

        for tool in self._all_tools:
            entries = rules_by_tool.get(tool.name, [])
            if not entries:
                allowed.append(tool)
                continue

            entries.sort(key=lambda x: x[0], reverse=True)

            is_allowed = True
            is_hidden = False
            is_forced = False
            is_prevent_stop = False
            reason: str | None = None

            for priority, rule in entries:
                if not rule.allowed:
                    is_allowed = False
                if rule.hidden:
                    is_hidden = True
                if rule.forced:
                    is_forced = True
                if rule.prevent_stop:
                    is_prevent_stop = True
                    prevent_step_refs.append(
                        {
                            "rule": {
                                "target": rule.target,
                                "allowed": rule.allowed,
                                "reason": rule.reason,
                            },
                            "priority": priority,
                        }
                    )
                if rule.reason:
                    reason = rule.reason

            if is_hidden:
                is_allowed = False

            if reason:
                reason_by_tool[tool.name] = reason

            if is_allowed:
                allowed.append(tool)
                max_priority = entries[0][0]
                if is_forced and (not forced or forced_priority < max_priority):
                    forced = tool
                    forced_priority = max_priority

            if not is_allowed and reason:
                reasons.append(f"- {tool.name}: {reason}")

            if is_hidden:
                hidden.append(tool)
            if is_prevent_stop:
                prevent_stop = True

        # Constrained: restrict allowed to forced + final_answer when forced
        if not self._unconstrained and forced is not None:
            allowed = [forced]
            if self._final_answer is not forced:
                allowed.append(self._final_answer)

        if prevent_stop and not isinstance(forced, FinalAnswerTool):
            if self._final_answer in allowed:
                allowed.remove(self._final_answer)
            if self._unconstrained:
                reasons.append("Do NOT call 'final_answer' yet — there are required steps remaining.")

        if not allowed:
            raise AgentError(
                "One of the generated rules is preventing the agent from continuing. "
                "This indicates that the provided requirements may conflict with each other. "
                "See the following rules that are preventing the agent from continuing.\n"
                + json.dumps(prevent_step_refs, indent=2, default=str)
            )

        # Unconstrained: build prompt-based constraint text
        constraint_text = None
        if self._unconstrained:
            if forced is not None and forced is not self._final_answer:
                reasons.insert(0, f"You MUST call '{forced.name}' in your next response.")
            unavailable = [r for r in reasons if r.startswith("- ")]
            directives = [r for r in reasons if not r.startswith("- ")]
            constraint_parts: list[str] = []
            if directives:
                constraint_parts.extend(directives)
            if unavailable:
                constraint_parts.append("The following tools are currently unavailable:")
                constraint_parts.extend(unavailable)
            constraint_text = "\n".join(constraint_parts) if constraint_parts else None

        # Constrained: compute tool_choice for forcing
        tool_choice: AnyTool | str = "auto"
        if not self._unconstrained:
            if forced is not None:
                tool_choice = forced
            elif len(allowed) == 1:
                tool_choice = allowed[0]
            else:
                tool_choice = "required"
            if (
                not isinstance(tool_choice, Tool)
                and not self._force_final_answer_as_tool
                and not prevent_stop
            ):
                tool_choice = "auto"

        return RequirementEvaluation(
            allowed_tools=allowed,
            hidden_tools=hidden,
            forced_tool=forced,
            can_stop=not prevent_stop,
            constraint_text=constraint_text,
            tool_choice=tool_choice,
            reason_by_tool=reason_by_tool,
            all_tools=list(self._all_tools),
        )

    def _increment_iteration(self) -> None:
        self._state.iteration += 1

        if self._run_config.max_iterations and self._state.iteration > self._run_config.max_iterations:
            raise AgentError(f"Agent was not able to resolve the task in {self._state.iteration} iterations.")

    def _create_final_answer_stream(self) -> StreamToolCallMiddleware:
        stream_middleware = StreamToolCallMiddleware(
            self._final_answer,
            "response",
            match_nested=False,
            force_streaming=False,
        )
        stream_middleware.emitter.on(
            "update",
            lambda data, meta: self._ctx.emitter.emit(
                "final_answer",
                ReasoningAgentFinalAnswerEvent(
                    state=self._state, output=data.output, delta=data.delta, output_structured=None
                ),
            ),
        )
        return stream_middleware

    async def _run_llm(self, evaluation: RequirementEvaluation) -> ChatModelOutput:
        stream_middleware = self._create_final_answer_stream()

        try:
            messages, options = self._prepare_llm_request(evaluation)
            response = await self._llm.run(messages, **options).middleware(stream_middleware)

            self._state.usage.merge(response.usage)
            self._state.cost.merge(response.cost)

            return response
        except ChatModelToolCallError as e:
            generated_content = e.generated_content or (e.response.get_text_content() if e.response else "")
            if not generated_content:
                raise e

            response = ChatModelOutput.from_chunks([e.response] if e.response else [])
            response.output.clear()
            response.output.append(AssistantMessage(generated_content))
            return response
        finally:
            stream_middleware.unbind()

    def _create_system_message(
        self,
        tool_constraints: str | None = None,
        tools: list[ReasoningAgentToolTemplateDefinition] | None = None,
    ) -> SystemMessage:
        return SystemMessage(
            self._templates.system.render(
                final_answer_name=self._final_answer.name,
                final_answer_schema=(
                    to_json(
                        self._final_answer.input_schema.model_json_schema(mode="validation"),
                        indent=2,
                        sort_keys=False,
                    )
                    if self._final_answer.custom_schema
                    else None
                ),
                final_answer_instructions=self._final_answer.instructions,
                tool_constraints=tool_constraints,
                tools=tools or [],
            )
        )

    def _prepare_llm_request(
        self, evaluation: RequirementEvaluation
    ) -> tuple[list[AnyMessage], ChatModelOptions]:
        if self._unconstrained:
            messages = [
                self._create_system_message(tool_constraints=evaluation.constraint_text),
                *self._state.memory.messages,
            ]
            tools_for_llm = [t for t in evaluation.allowed_tools if t not in evaluation.hidden_tools]
            options = ChatModelOptions(
                max_retries=self._run_config.max_retries_per_step,
                tools=tools_for_llm,
                tool_choice="auto",
                stream_partial_tool_calls=True,
                fallback_tool=self._final_answer if evaluation.can_stop else None,
            )
            cache_index = 0
        else:
            tool_defs = [
                ReasoningAgentToolTemplateDefinition.from_tool(
                    tool,
                    allowed=tool in evaluation.allowed_tools,
                    reason=evaluation.reason_by_tool.get(tool.name),
                )
                for tool in evaluation.all_tools
                if tool not in evaluation.hidden_tools
            ]
            messages = [
                self._create_system_message(tools=tool_defs),
                *self._state.memory.messages,
            ]
            tools_for_llm = [t for t in evaluation.allowed_tools if t not in evaluation.hidden_tools]
            options = ChatModelOptions(
                max_retries=self._run_config.max_retries_per_step,
                tools=tools_for_llm,
                tool_choice=evaluation.tool_choice,
                stream_partial_tool_calls=True,
                fallback_tool=self._final_answer if evaluation.can_stop else None,
            )
            cache_index = 1 if self._requirements else 0

        cache_control_injection_points = [
            {"location": "message", "index": cache_index},
            {
                "location": "message",
                "index": find_last_index(
                    messages,
                    lambda msg: (
                        not msg.meta.get(TEMP_MESSAGE_META_KEY)
                        and (self._llm.provider_id != "amazon_bedrock" or not isinstance(msg, ToolMessage))
                    ),
                ),
            },
        ]
        options["cache_control_injection_points"] = ensure_strictly_increasing(  # type: ignore
            cache_control_injection_points,
            key=lambda v: v["index"],
        )
        return messages, options

    async def _create_final_answer_tool_call(self, full_text: str) -> AssistantMessage | None:
        json_object_pair = find_first_pair(full_text, ("{", "}"))
        final_answer_input = parse_broken_json(json_object_pair.outer) if json_object_pair else None
        if not final_answer_input and not self._final_answer.custom_schema:
            final_answer_input = FinalAnswerToolSchema(response=full_text).model_dump()

        if not final_answer_input:
            return None

        manual_assistant_tool_call_message = MessageToolCallContent(
            type="tool-call",
            id=f"call_{generate_random_string(8).lower()}",
            tool_name=self._final_answer.name,
            args=to_json(final_answer_input, sort_keys=False),
        )
        return AssistantMessage(manual_assistant_tool_call_message)

    async def _invoke_tool_calls(
        self, tool_calls: list[MessageToolCallContent], evaluation: RequirementEvaluation
    ) -> list[ToolMessage]:
        tool_results: list[ToolMessage] = []

        for tool_call in await run_tools(
            tools=evaluation.allowed_tools,
            messages=tool_calls,
            context={"state": self._state.model_dump()},
        ):
            self._state.steps.append(
                ReasoningAgentRunStateStep(
                    id=str(uuid.uuid4()),
                    iteration=self._state.iteration,
                    input=tool_call.input,
                    output=tool_call.output,
                    tool=tool_call.tool,
                    error=tool_call.error,
                )
            )

            if tool_call.error is not None:
                result = self._templates.tool_error.render(
                    ReasoningAgentToolErrorPromptInput(reason=tool_call.error.explain())
                )
            else:
                result = (
                    tool_call.output.get_text_content()
                    if not tool_call.output.is_empty()
                    else self._templates.tool_no_result.render(tool_call=tool_call)
                )

            tool_results.append(
                ToolMessage(
                    MessageToolResultContent(
                        tool_name=tool_call.tool.name if tool_call.tool else tool_call.msg.tool_name,
                        tool_call_id=tool_call.msg.id,
                        result=result,
                    )
                )
            )
            if tool_call.error is not None:
                self._iteration_error_counter.use(tool_call.error)
                self._global_error_counter.use(tool_call.error)

        return tool_results

    async def add_messages(self, messages: list[AnyMessage]) -> None:
        await self._state.memory.add_many(messages)

    async def run(self) -> ReasoningAgentRunState:
        if self._state.answer is not None:
            return self._state

        await self._init_requirements()

        while self._state.answer is None:
            self._increment_iteration()

            evaluation = await self._evaluate_requirements()
            await self._ctx.emitter.emit(
                "start",
                ReasoningAgentStartEvent(state=self._state, evaluation=evaluation),
            )
            self._iteration_error_counter.reset()

            if self._unconstrained:
                response = await self._run_unconstrained(evaluation)
            else:
                response = await self._run_constrained(evaluation)

            await self._ctx.emitter.emit(
                "success",
                ReasoningAgentSuccessEvent(state=self._state, response=response),
            )
        return self._state

    async def _run_constrained(self, evaluation: RequirementEvaluation) -> ChatModelOutput:
        response = await self._run_llm(evaluation)

        if not response.get_tool_calls():
            text = response.get_text_content()
            final_answer_tool_call = (
                await self._create_final_answer_tool_call(text) if evaluation.can_stop and text else None
            )
            if final_answer_tool_call:
                stream = self._create_final_answer_stream()
                await stream.add(ChatModelOutput(output=[final_answer_tool_call]))
                response.output_structured = None
                response.output = [final_answer_tool_call]
            else:
                err = AgentError("Model produced an invalid final answer tool call.")
                self._iteration_error_counter.use(err)
                self._global_error_counter.use(err)

                if not evaluation.can_stop:
                    return await self._run_constrained(evaluation)

                self._requirements = []
                updated = await self._evaluate_requirements(
                    extra_rules=[
                        Rule(target=self._final_answer.name, allowed=True, hidden=False),
                    ],
                )
                self._force_final_answer_as_tool = True
                return await self._run_constrained(updated)

        tool_calls = response.get_tool_calls()
        for tool_call_msg in tool_calls:
            self._tool_call_cycle_checker.register(tool_call_msg)
            if self._tool_call_cycle_checker.cycle_found:
                self._tool_call_cycle_checker.reset()
                updated = await self._evaluate_requirements(
                    extra_rules=[
                        Rule(
                            target=tool_call_msg.tool_name,
                            allowed=False,
                            hidden=False,
                            forced=True,
                        ),
                    ],
                )
                return await self._run_constrained(updated)

        tool_results = await self._invoke_tool_calls(tool_calls, evaluation)

        await self._state.memory.add_many([*response.output, *tool_results])
        await delete_messages_by_meta_key(self._state.memory, key=TEMP_MESSAGE_META_KEY, value=True)

        return response

    async def _run_unconstrained(self, evaluation: RequirementEvaluation) -> ChatModelOutput:
        response = await self._run_llm(evaluation)

        if not response.get_tool_calls():
            text = response.get_text_content()
            final_answer_tool_call = await self._create_final_answer_tool_call(text) if text else None
            if final_answer_tool_call:
                stream = self._create_final_answer_stream()
                await stream.add(ChatModelOutput(output=[final_answer_tool_call]))
                response.output_structured = None
                response.output = [final_answer_tool_call]
            elif not self._force_final_answer_as_tool:
                self._state.answer = AssistantMessage(text or "")
                self._state.result = text
                await self._state.memory.add_many(response.output)
                return response
            else:
                err = AgentError("Model produced text instead of calling final_answer tool.")
                self._iteration_error_counter.use(err)
                self._global_error_counter.use(err)
                await self._state.memory.add_many(response.output)

                await self._state.memory.add(
                    UserMessage(
                        "Please provide your final answer using the 'final_answer' tool.",
                        meta={TEMP_MESSAGE_META_KEY: True},
                    )
                )
                return response

        tool_calls = response.get_tool_calls()
        for tool_call_msg in tool_calls:
            self._tool_call_cycle_checker.register(tool_call_msg)
            if self._tool_call_cycle_checker.cycle_found:
                self._tool_call_cycle_checker.reset()
                await self._state.memory.add_many(response.output)

                await self._state.memory.add(
                    UserMessage(
                        f"You appear to be calling '{tool_call_msg.tool_name}' repeatedly "
                        "with the same input. Break the cycle by using a different tool "
                        "or different input, or call 'final_answer' to provide your final answer.",
                        meta={TEMP_MESSAGE_META_KEY: True},
                    )
                )
                return response

        tool_results = await self._invoke_tool_calls(tool_calls, evaluation)

        await self._state.memory.add_many([*response.output, *tool_results])
        await delete_messages_by_meta_key(self._state.memory, key=TEMP_MESSAGE_META_KEY, value=True)

        return response
