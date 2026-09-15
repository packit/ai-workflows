from beeai_framework.agents.requirement.requirements.conditional import ConditionalRequirement
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools.think import ThinkTool

from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    render_template,
)
from ymir.common.logging_setup import get_trajectory_writeable


def get_instructions() -> str:
    return render_template("title/instructions.j2")


def get_prompt() -> str:
    return "title/prompt.j2"


def create_title_agent() -> ReasoningAgent:
    return ReasoningAgent(
        name="TitleAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[ThinkTool()],
        memory=UnconstrainedMemory(),
        requirements=[ConditionalRequirement(ThinkTool, force_at_step=1)],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(),
    )
