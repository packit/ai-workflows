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
from ymir.common.models import BuildInstructionsInput
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.filesystem import GetCWDTool
from ymir.tools.unprivileged.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    SearchTextTool,
    StrReplaceTool,
    ViewTool,
)


def get_instructions(*, has_extract_log_snippets: bool = False) -> str:
    return render_template(
        "build/instructions.j2",
        BuildInstructionsInput(has_extract_log_snippets=has_extract_log_snippets),
    )


def get_prompt() -> str:
    return "build/prompt.j2"


def create_build_agent(mcp_tools: list[Tool], local_tool_options: dict[str, Any]) -> ReasoningAgent:
    filtered_mcp_tools = [
        t for t in mcp_tools if t.name in ["build_package", "download_artifacts", "extract_log_snippets"]
    ]
    available_tool_names = {t.name for t in filtered_mcp_tools}
    has_extract_log_snippets = "extract_log_snippets" in available_tool_names

    requirements = [
        ConditionalRequirement(
            ThinkTool,
            force_at_step=1,
            force_after=Tool,
            consecutive_allowed=False,
            only_success_invocations=False,
        ),
        ConditionalRequirement("build_package", min_invocations=1),
        ConditionalRequirement("download_artifacts", only_after=["build_package"]),
    ]
    if "extract_log_snippets" in available_tool_names:
        requirements.append(
            ConditionalRequirement("extract_log_snippets", only_after=["download_artifacts"]),
        )

    return ReasoningAgent(
        name="BuildAgent",
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
            *filtered_mcp_tools,
        ],
        memory=UnconstrainedMemory(),
        requirements=requirements,
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(has_extract_log_snippets=has_extract_log_snippets),
    )
