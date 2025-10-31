import asyncio
import copy
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.agents.requirement.prompts import RequirementAgentSystemPrompt
from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from common.models import (
    BackportVerificationInputSchema,
    BackportVerificationOutputSchema,
)
from observability import setup_observability
from tools.commands import RunShellCommandTool
from tools.filesystem import GetCWDTool
from tools.text import (
    ViewTool,
    SearchTextTool,
)
from tools.wicked_git import GitLogSearchTool
from utils import get_agent_execution_config, get_chat_model, mcp_tools, render_prompt

logger = logging.getLogger(__name__)


def get_instructions() -> str:
    return """
      You are an expert at verifying backported patches in RHEL dist-git repositories.

      Your task is to verify that a backport was done correctly by checking the following:

      1. **Patch File Existence**: Check if a patch file for the Jira issue exists in the dist-git repository.
         The expected patch name is `<JIRA_ISSUE>.patch`.

      2. **Patch in Spec File**: Verify that the patch is properly referenced in the RPM spec file:
         - Check for a `Patch<N>:` tag pointing to the patch file
         - Verify the patch is applied in the `%prep` section with a correct `-p` flag, usually `-p1`
         - Ensure the patch numbering is sequential with existing patches

      3. **Git Log Search**: Use the `git_log_search` tool to check if the Jira issue was mentioned
         in recent commits. This helps verify the backport was committed properly.

      4. **Patch Application**: Use `centpkg prep` to verify that the patch applies cleanly during
         package preparation. Check for any .rej (reject) files that would indicate patch conflicts
         or the git conflict marks inside the patch file.

      5. **Patch Content Comparison**: If the patch URL is accessible, download it and compare with
         the patch in the repository to ensure the correct fix was backported.
         - All core functionality of the upstream patch was backported, especially for libraries and binaries
           - Corrected variable assignments, function calls, or logic flows in code files
           - Missing error-handling or validation checks that prevent a vulnerability
           - Additional boundary checks to prevent buffer overflows or other memory-related issues
           - Changes to configuration files or build system files only if they are essential for the fix
           - The patch should not introduce "unresolved symbol" errors during compilation
         - All user-facing documentation was backported
         - Ensure that all changes that were skipped during the backport were properly justified
         - For CVEs, ensure the patch correctly mitigates the security problem
         - No regressions were introduced
         - No unintended omissions
         - No unintended changes were introduced

      Document all findings clearly, noting both what was done correctly and any issues found.
      Provide specific recommendations for fixing any problems discovered.

      General instructions:
      - Use the `view` tool to examine spec files and patch files
      - Use the `search_text` tool to find patch references in spec files
      - Use `run_shell_command` for centpkg operations
      - Be thorough in your verification and document everything you check
    """


def get_prompt() -> str:
    return """
      Your working directory is {{local_clone}}, a clone of the dist-git repository for package {{package}}.

      Verify that the backport for Jira issue {{jira_issue}} was done correctly.
      The upstream patch URL is: {{upstream_fix}}

      Check all aspects of the backport:
      1. Patch file existence and content
      2. Spec file updates (Patch tag and %prep section)
      3. Git commit history
      4. Patch applies cleanly during prep
      5. Backported all core functionality of the upstream patch

      Document your findings and determine if the backport was done correctly.
    """


def create_backport_verification_agent(
    _: list[Tool], local_tool_options: dict[str, Any]
) -> RequirementAgent:
    return RequirementAgent(
        name="BackportVerificationAgent",
        llm=get_chat_model(),
        tools=[
            ThinkTool(),
            DuckDuckGoSearchTool(),
            RunShellCommandTool(options=local_tool_options),
            ViewTool(options=local_tool_options),
            SearchTextTool(options=local_tool_options),
            GetCWDTool(options=local_tool_options),
            GitLogSearchTool(options=local_tool_options),
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
        role="Red Hat Enterprise Linux maintainer",
        instructions=get_instructions(),
        templates={"system": copy.deepcopy(RequirementAgentSystemPrompt)},
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ.get("COLLECTOR_ENDPOINT", "http://localhost:4318"))

    local_tool_options = {"working_directory": None}

    async def run_verification(
        package: str,
        jira_issue: str,
        upstream_fix: str,
    ):
        local_clone = Path(os.environ["GIT_REPO_BASEPATH"]) / jira_issue / package

        local_tool_options["working_directory"] = local_clone

        async with mcp_tools(os.environ.get("MCP_GATEWAY_URL", "http://localhost:8000")) as gateway_tools:
            verification_agent = create_backport_verification_agent(gateway_tools, local_tool_options)

            response = await verification_agent.run(
                render_prompt(
                    template=get_prompt(),
                    input=BackportVerificationInputSchema(
                        local_clone=local_clone,
                        package=package,
                        jira_issue=jira_issue,
                        upstream_fix=upstream_fix,
                    ),
                ),
                expected_output=BackportVerificationOutputSchema,
                **get_agent_execution_config(),
            )

            verification_result = BackportVerificationOutputSchema.model_validate_json(
                response.last_message.text
            )
            return verification_result

    # Support running in direct mode with environment variables
    if (
        (package := os.getenv("PACKAGE", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
        and (upstream_fix := os.getenv("UPSTREAM_FIX", None))
    ):
        logger.info("Running in direct mode with environment variables")
        result = await run_verification(
            package=package,
            jira_issue=jira_issue,
            upstream_fix=upstream_fix,
        )
        logger.info(f"Verification completed: {result.model_dump_json(indent=4)}")
        sys.exit(0)
    else:
        logger.error(
            "Required environment variables not set. Need: PACKAGE, JIRA_ISSUE, UPSTREAM_FIX"
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
