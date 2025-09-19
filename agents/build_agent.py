import copy
from typing import Any

from beeai_framework.agents.experimental import RequirementAgent
from beeai_framework.agents.experimental.prompts import RequirementAgentSystemPrompt
from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from tools.commands import RunShellCommandTool
from tools.text import CreateTool, InsertTool, InsertAfterSubstringTool, StrReplaceTool, ViewTool
from utils import get_chat_model


def get_instructions() -> str:
    return """
      You are an expert on building packages in RHEL ecosystem and analyzing build failures.

      Build a package using the `build_package` tool. If the build succeeded, your work is done.
      If the build failed, download all *.log.gz files returned in `artifacts_urls`, if any,
      using the `download_artifacts` tool to the current directory. If there are no log files,
      just return the error message. Otherwise, start with `builder-live.log` and try to identify
      the build failure. If not found, try the same with `root.log`. Summarize the findings
      and return them as `error`.
    """


def get_prompt() -> str:
    return """
      Build the SRPM {{srpm_path}}, use {{dist_git_branch}} dist-git branch and {{jira_issue}} Jira issue.
    """


def create_build_agent(mcp_tools: list[Tool], local_tool_options: dict[str, Any]) -> RequirementAgent:
    return RequirementAgent(
        name="BuildAgent",
        llm=get_chat_model(),
        tools=[
            ThinkTool(),
            DuckDuckGoSearchTool(),
            RunShellCommandTool(options=local_tool_options),
            CreateTool(options=local_tool_options),
            ViewTool(options=local_tool_options),
            InsertTool(options=local_tool_options),
            InsertAfterSubstringTool(options=local_tool_options),
            StrReplaceTool(options=local_tool_options),
        ] + [t for t in mcp_tools if t.name in ["build_package", "download_artifacts"]],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(ThinkTool, force_at_step=1, force_after=Tool, consecutive_allowed=False),
            ConditionalRequirement("build_package", min_invocations=1),
            ConditionalRequirement("download_artifacts", only_after="build_package"),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(),
        # role and instructions above set defaults for the system prompt input
        # but the `RequirementAgentSystemPrompt` instance is shared so the defaults
        # affect all requirement agents - use our own copy to prevent that
        templates={"system": copy.deepcopy(RequirementAgentSystemPrompt)},
    )
