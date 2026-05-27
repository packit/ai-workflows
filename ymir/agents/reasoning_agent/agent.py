from collections.abc import Sequence
from typing import Any

from beeai_framework.agents import AgentExecutionConfig, AgentMeta, AgentOptions, BaseAgent
from beeai_framework.agents.requirement.agent import RequirementAgentRequirement
from beeai_framework.agents.tool_calling.utils import ToolCallChecker, ToolCallCheckerConfig
from beeai_framework.backend import AnyMessage
from beeai_framework.backend.chat import ChatModel
from beeai_framework.backend.message import MessageTextContent, UserMessage
from beeai_framework.context import RunContext, RunMiddlewareType
from beeai_framework.emitter import Emitter
from beeai_framework.memory.base_memory import BaseMemory
from beeai_framework.memory.unconstrained_memory import UnconstrainedMemory
from beeai_framework.memory.utils import extract_last_tool_call_pair
from beeai_framework.runnable import runnable_entry
from beeai_framework.template import PromptTemplate
from beeai_framework.tools import AnyTool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.utils.dicts import exclude_none
from beeai_framework.utils.lists import cast_list
from beeai_framework.utils.models import update_model
from typing_extensions import Unpack

from ymir.agents.reasoning_agent._runner import ReasoningAgentRunner
from ymir.agents.reasoning_agent.events import reasoning_agent_event_types
from ymir.agents.reasoning_agent.prompts import ReasoningAgentTaskPromptInput
from ymir.agents.reasoning_agent.types import (
    ReasoningAgentOutput,
    ReasoningAgentTemplateFactory,
    ReasoningAgentTemplates,
    ReasoningAgentTemplatesKeys,
)


class ReasoningAgent(BaseAgent[ReasoningAgentOutput]):
    """
    Drop-in replacement for RequirementAgent that is also compatible with
    reasoning models (e.g., Anthropic extended thinking, OpenAI o-series).

    When ``unconstrained=False`` (default), behaves like RequirementAgent:
    requirements are evaluated each iteration and ThinkTool is available.

    When ``unconstrained=True``, requirements and ThinkTool are ignored and
    tool_choice is always "auto", which is required by reasoning models.
    """

    def __init__(
        self,
        *,
        llm: ChatModel | str,
        memory: BaseMemory | None = None,
        tools: Sequence[AnyTool] | None = None,
        requirements: Sequence[RequirementAgentRequirement] | None = None,
        unconstrained: bool = False,
        name: str | None = None,
        description: str | None = None,
        role: str | None = None,
        instructions: str | list[str] | None = None,
        notes: str | list[str] | None = None,
        tool_call_checker: ToolCallCheckerConfig | bool = True,
        final_answer_as_tool: bool = True,
        save_intermediate_steps: bool = True,
        templates: dict[ReasoningAgentTemplatesKeys, PromptTemplate[Any] | ReasoningAgentTemplateFactory]
        | ReasoningAgentTemplates
        | None = None,
        middlewares: list[RunMiddlewareType] | None = None,
    ) -> None:
        super().__init__(middlewares=middlewares)
        self._llm = ChatModel.from_name(llm) if isinstance(llm, str) else llm
        self._memory = memory or UnconstrainedMemory()
        self._templates = self._generate_templates(templates)
        self._save_intermediate_steps = save_intermediate_steps
        self._tool_call_checker = tool_call_checker
        self._final_answer_as_tool = final_answer_as_tool
        self._unconstrained = unconstrained
        self._requirements = [] if unconstrained else list(requirements or [])
        if role or instructions or notes:
            self._templates.system.update(
                defaults=exclude_none(
                    {
                        "role": role,
                        "instructions": "\n -".join(cast_list(instructions)) if instructions else None,
                        "notes": "\n -".join(cast_list(notes)) if notes else None,
                    }
                )
            )
        tools_list = list(tools or [])
        if unconstrained:
            tools_list = [t for t in tools_list if not isinstance(t, ThinkTool)]
        self._tools = tools_list
        self._meta = AgentMeta(name=name or "", description=description or "", tools=self._tools)
        self.runner_cls: type[ReasoningAgentRunner] = ReasoningAgentRunner

    @runnable_entry
    async def run(
        self, input: str | list[AnyMessage], /, **kwargs: Unpack[AgentOptions]
    ) -> ReasoningAgentOutput:
        runner = self.runner_cls(
            llm=self._llm,
            config=AgentExecutionConfig(
                max_retries_per_step=kwargs.get("max_retries_per_step", 3),
                total_max_retries=kwargs.get("total_max_retries", 20),
                max_iterations=kwargs.get("max_iterations", 20),
            ),
            tools=self._tools,
            expected_output=kwargs.get("expected_output"),
            tool_call_cycle_checker=self._create_tool_call_checker(),
            run_context=RunContext.get(),
            force_final_answer_as_tool=self._final_answer_as_tool,
            templates=self._templates,
            requirements=self._requirements,
            unconstrained=self._unconstrained,
        )
        new_messages = self._process_input(
            input,
            backstory=kwargs.get("backstory"),
            expected_output=kwargs.get("expected_output"),
        )
        await runner.add_messages(self.memory.messages)
        await runner.add_messages(new_messages)

        final_state = await runner.run()

        if self._save_intermediate_steps:
            self.memory.reset()
            await self.memory.add_many(final_state.memory.messages)
        else:
            await self.memory.add_many(new_messages)
            await self.memory.add_many(extract_last_tool_call_pair(final_state.memory) or [])

        if final_state.answer is None:
            raise ValueError("reasoning agent finished without producing an answer")
        return ReasoningAgentOutput(
            output=[final_state.answer],
            output_structured=final_state.result,
            state=final_state,
        )

    def _process_input(
        self, input: str | list[AnyMessage], backstory: str | None, expected_output: Any
    ) -> list[AnyMessage]:
        if not input:
            return []

        *msgs, last_message = [UserMessage(input)] if isinstance(input, str) else input
        if last_message is not None and isinstance(last_message, UserMessage) and last_message.text:
            user_message = UserMessage(
                self._templates.task.render(
                    ReasoningAgentTaskPromptInput(
                        prompt=last_message.text,
                        context=backstory,
                        expected_output=expected_output if isinstance(expected_output, str) else None,
                    )
                ),
                meta=last_message.meta.copy(),
            )
            user_message.content.extend(
                [content for content in last_message.content if not isinstance(content, MessageTextContent)]
            )
            return [*msgs, user_message]
        return msgs if last_message is None else [*msgs, last_message]

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["agent", "reasoning"], creator=self, events=reasoning_agent_event_types
        )

    @property
    def memory(self) -> BaseMemory:
        return self._memory

    @memory.setter
    def memory(self, memory: BaseMemory) -> None:
        self._memory = memory

    @staticmethod
    def _generate_templates(
        overrides: dict[ReasoningAgentTemplatesKeys, PromptTemplate[Any] | ReasoningAgentTemplateFactory]
        | ReasoningAgentTemplates
        | None = None,
    ) -> ReasoningAgentTemplates:
        if isinstance(overrides, ReasoningAgentTemplates):
            return overrides

        templates = ReasoningAgentTemplates()
        if overrides is None:
            return templates

        for name in ReasoningAgentTemplates.model_fields:
            override: PromptTemplate[Any] | ReasoningAgentTemplateFactory | None = overrides.get(name)
            if override is None:
                continue
            if isinstance(override, PromptTemplate):
                setattr(templates, name, override)
            else:
                setattr(templates, name, override(getattr(templates, name)))
        return templates

    async def clone(self) -> "ReasoningAgent":
        cloned = ReasoningAgent(
            llm=await self._llm.clone(),
            memory=await self._memory.clone(),
            tools=self._tools.copy(),
            requirements=self._requirements.copy(),
            unconstrained=self._unconstrained,
            templates=self._templates.model_dump(),
            tool_call_checker=(
                self._tool_call_checker.config.model_copy()
                if isinstance(self._tool_call_checker, ToolCallChecker)
                else self._tool_call_checker
            ),
            save_intermediate_steps=self._save_intermediate_steps,
            final_answer_as_tool=self._final_answer_as_tool,
            name=self._meta.name,
            description=self._meta.description,
            middlewares=self.middlewares.copy(),
        )
        cloned.emitter = await self.emitter.clone()
        cloned.runner_cls = self.runner_cls
        return cloned

    @property
    def meta(self) -> AgentMeta:
        parent = super().meta

        return AgentMeta(
            name=self._meta.name or parent.name,
            description=self._meta.description or parent.description,
            extra_description=self._meta.extra_description or parent.extra_description,
            tools=list(self._tools),
        )

    def _create_tool_call_checker(self) -> ToolCallChecker:
        config = ToolCallCheckerConfig()
        update_model(config, sources=[self._tool_call_checker])

        instance = ToolCallChecker(config)
        instance.enabled = self._tool_call_checker is not False
        return instance
