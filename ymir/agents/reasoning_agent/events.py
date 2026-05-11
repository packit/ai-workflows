from typing import Any

from beeai_framework.backend import ChatModelOutput
from pydantic import BaseModel, ConfigDict

from ymir.agents.reasoning_agent.types import ReasoningAgentRunState, RequirementEvaluation


class ReasoningAgentStartEvent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: ReasoningAgentRunState
    evaluation: RequirementEvaluation


class ReasoningAgentSuccessEvent(BaseModel):
    state: ReasoningAgentRunState
    response: ChatModelOutput


class ReasoningAgentFinalAnswerEvent(BaseModel):
    state: ReasoningAgentRunState
    output_structured: BaseModel | Any
    output: str
    delta: str


reasoning_agent_event_types: dict[str, type] = {
    "start": ReasoningAgentStartEvent,
    "success": ReasoningAgentSuccessEvent,
    "final_answer": ReasoningAgentFinalAnswerEvent,
}
