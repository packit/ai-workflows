from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from beeai_framework.agents import AgentOutput
from beeai_framework.backend import AssistantMessage, UserMessage
from beeai_framework.backend.types import ChatModelCost, ChatModelUsage
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import BaseMemory
from beeai_framework.template import PromptTemplate
from beeai_framework.tools import AnyTool, Tool, ToolOutput
from pydantic import BaseModel, ConfigDict, Field, InstanceOf

from ymir.agents.reasoning_agent.prompts import (
    ReasoningAgentSystemPrompt,
    ReasoningAgentSystemPromptInput,
    ReasoningAgentTaskPrompt,
    ReasoningAgentTaskPromptInput,
    ReasoningAgentToolErrorPrompt,
    ReasoningAgentToolErrorPromptInput,
    ReasoningAgentToolNoResultPrompt,
    ReasoningAgentToolNoResultTemplateInput,
)


class ReasoningAgentTemplates(BaseModel):
    system: InstanceOf[PromptTemplate[ReasoningAgentSystemPromptInput]] = Field(
        default_factory=lambda: ReasoningAgentSystemPrompt.fork(None),
    )
    task: InstanceOf[PromptTemplate[ReasoningAgentTaskPromptInput]] = Field(
        default_factory=lambda: ReasoningAgentTaskPrompt.fork(None),
    )
    tool_error: InstanceOf[PromptTemplate[ReasoningAgentToolErrorPromptInput]] = Field(
        default_factory=lambda: ReasoningAgentToolErrorPrompt.fork(None),
    )
    tool_no_result: InstanceOf[PromptTemplate[ReasoningAgentToolNoResultTemplateInput]] = Field(
        default_factory=lambda: ReasoningAgentToolNoResultPrompt.fork(None),
    )


ReasoningAgentTemplateFactory = Callable[[InstanceOf[PromptTemplate[Any]]], InstanceOf[PromptTemplate[Any]]]
ReasoningAgentTemplatesKeys = Annotated[str, lambda v: v in ReasoningAgentTemplates.model_fields]


class ReasoningAgentRunStateStep(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    iteration: int
    tool: InstanceOf[Tool[Any, Any, Any]] | None
    input: Any
    output: InstanceOf[ToolOutput]
    error: InstanceOf[FrameworkError] | None


class ReasoningAgentRunState(BaseModel):
    answer: InstanceOf[AssistantMessage] | None = None
    result: Any
    memory: InstanceOf[BaseMemory]
    iteration: int
    steps: list[ReasoningAgentRunStateStep] = []
    usage: ChatModelUsage = ChatModelUsage()
    cost: ChatModelCost = ChatModelCost()

    @property
    def input(self) -> UserMessage:
        return next(msg for msg in reversed(self.memory.messages) if isinstance(msg, UserMessage))


class ReasoningAgentOutput(AgentOutput):
    state: ReasoningAgentRunState


@dataclass
class RequirementEvaluation:
    allowed_tools: list[AnyTool] = field(default_factory=list)
    hidden_tools: list[AnyTool] = field(default_factory=list)
    forced_tool: AnyTool | None = None
    can_stop: bool = True
    constraint_text: str | None = None
    tool_choice: AnyTool | str = "auto"
    reason_by_tool: dict[str, str | None] = field(default_factory=dict)
    all_tools: list[AnyTool] = field(default_factory=list)
