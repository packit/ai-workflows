import copy
from typing import Any

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.agents.requirement.prompts import RequirementAgentSystemPrompt
from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from tools.commands import RunShellCommandTool
from tools.specfile import AddChangelogEntryTool
from tools.filesystem import GetCWDTool
from tools.text import (
    CreateTool,
    InsertTool,
    InsertAfterSubstringTool,
    StrReplaceTool,
    ViewTool,
    SearchTextTool,
)
from utils import get_chat_model, get_tool_call_checker_config


def get_instructions() -> str:
    return """
      You are an expert on summarizing packaging changes in RHEL ecosystem.

      To document a change corresponding to <JIRA_ISSUE> Jira issue, having a brief summary
      of changes performed, do the following:

      1. Run `git diff --cached --stat` to see which files have been changed.

      2. Examine changes in each file by running `git diff --cached -- <filename>`.

         IMPORTANT: Do not run `git diff --cached` without specifying a path, as patch files can be very large
         and could overflow the context. Always examine files one by one.

      3. Add a new changelog entry to the spec file. Use the `add_changelog_entry` tool.
         Examine the previous changelog entries and try to use the same style. In general,
         the entry should contain a short summary of the changes, ideally fitting on a single line,
         and a line referencing the Jira issue. Use "- Resolves: <JIRA_ISSUE>" unless
         the spec file has historically used a different style.

         IMPORTANT: The changelog entry should focus on user-facing changes only. Do not mention
         technical packaging details such as added/removed patches, changed BuildRequires,
         or other spec file modifications that are not visible to end users.

      4. Generate a title for commit message and merge request. It should be descriptive
         but shouldn't be longer than 80 characters.

      5. Summarize the changes in a short paragraph that will be used as commit message
         and merge request description. Line length shouldn't exceed 80 characters.
         There is no need to reference the Jira issue, it will be appended later.


     General instructions:

      - Never change anything in the spec file changelog, you are only allowed to add a single changelog entry.
      - Prefer native tools, if available, the `run_shell_command` tool should be the last resort.
    """


def get_prompt() -> str:
    return """
      Document a packaging change done as part of {{jira_issue}} Jira issue, summarized as:

      {{changes_summary}}
    """


def create_log_agent(_: list[Tool], local_tool_options: dict[str, Any]) -> RequirementAgent:
    return RequirementAgent(
        name="LogAgent",
        llm=get_chat_model(),
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
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(),
        # role and instructions above set defaults for the system prompt input
        # but the `RequirementAgentSystemPrompt` instance is shared so the defaults
        # affect all requirement agents - use our own copy to prevent that
        templates={"system": copy.deepcopy(RequirementAgentSystemPrompt)},
    )
