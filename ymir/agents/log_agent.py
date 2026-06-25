from typing import Any

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    render_template,
)
from ymir.common.logging_setup import get_trajectory_writeable
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.filesystem import GetCWDTool
from ymir.tools.unprivileged.specfile import AddChangelogEntryTool
from ymir.tools.unprivileged.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    SearchTextTool,
    StrReplaceTool,
    ViewTool,
)


def get_instructions() -> str:
    return render_template("log/instructions.j2")


def get_prompt() -> str:
    return "log/prompt.j2"


def create_log_agent(_: list[Tool], local_tool_options: dict[str, Any]) -> ReasoningAgent:
    return ReasoningAgent(
        name="LogAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            DuckDuckGoSearchTool(),
            RunShellCommandTool(options=local_tool_options),
            CreateTool(options=local_tool_options),
            ViewTool(options=local_tool_options),
            InsertTool(options=local_tool_options),
            InsertAfterSubstringTool(options=local_tool_options),
            StrReplaceTool(options=local_tool_options),
            SearchTextTool(options=local_tool_options),
            GetCWDTool(options=local_tool_options),
            AddChangelogEntryTool(options=local_tool_options),
        ],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                force_after=Tool,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(),
    )
