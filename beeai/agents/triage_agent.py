import asyncio
import logging
import os
import sys
import traceback
from enum import Enum
from typing import Union

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.backend import ChatModel
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool

from base_agent import BaseAgent, TInputSchema, TOutputSchema
from observability import setup_observability
from tools.commands import RunShellCommandTool
from tools.patch_validator import PatchValidatorTool
from utils import mcp_tools, redis_client

logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    issue: str = Field(description="Jira issue identifier to analyze (e.g. RHEL-12345)")


class Resolution(Enum):
    REBASE = "rebase"
    BACKPORT = "backport"
    CLARIFICATION_NEEDED = "clarification-needed"
    NO_ACTION = "no-action"
    ERROR = "error"


class RebaseData(BaseModel):
    package: str = Field(description="Package name")
    version: str = Field(description="Target version")
    branch: str = Field(description="Target branch")
    jira_issue: str = Field(description="Jira issue identifier")


class BackportData(BaseModel):
    package: str = Field(description="Package name")
    branch: str = Field(description="Target branch")
    patch_url: str = Field(description="URL or reference to the source of the fix")
    justification: str = Field(description="Clear explanation of why this patch fixes the issue")
    jira_issue: str = Field(description="Jira issue identifier")


class ClarificationNeededData(BaseModel):
    findings: str = Field(description="Summary of the investigation")
    additional_info_needed: str = Field(description="Summary of missing information")
    jira_issue: str = Field(description="Jira issue identifier")


class NoActionData(BaseModel):
    reasoning: str = Field(description="Reason why the issue is intentionally non-actionable")
    jira_issue: str = Field(description="Jira issue identifier")


class ErrorData(BaseModel):
    details: str = Field(description="Specific details about an error")
    jira_issue: str = Field(description="Jira issue identifier")


class OutputSchema(BaseModel):
    resolution: Resolution = Field(description="Triage resolution")
    data: Union[RebaseData, BackportData, ClarificationNeededData, NoActionData, ErrorData] = Field(
        description="Associated data"
    )


class TriageAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            llm=ChatModel.from_name(os.getenv("CHAT_MODEL")),
            tools=[ThinkTool(), RunShellCommandTool(), PatchValidatorTool()],
            memory=UnconstrainedMemory(),
            requirements=[
                ConditionalRequirement(ThinkTool, force_after=Tool, consecutive_allowed=False),
                ConditionalRequirement("get_jira_details", min_invocations=1),
                ConditionalRequirement(RunShellCommandTool, only_after="get_jira_details"),
                ConditionalRequirement(PatchValidatorTool, only_after="get_jira_details"),
            ],
            middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        )

    @property
    def input_schema(self) -> type[TInputSchema]:
        return InputSchema

    @property
    def output_schema(self) -> type[TOutputSchema]:
        return OutputSchema

    @property
    def prompt(self) -> str:
        return """
          You are an agent tasked to analyze Jira issues for RHEL and identify the most efficient path to resolution,
          whether through a version rebase, a patch backport, or by requesting clarification when blocked.

          **Important**: This agent focuses on bugs, CVEs, and technical defects that need code fixes.
          QE tasks, feature requests, refactoring, documentation, and other non-bug issues should be marked as "no-action".

          Goal: Analyze the given issue to determine the correct course of action.

          **Initial Analysis Steps**

          1. Open the {{ issue }} Jira issue and thoroughly analyze it:
             * Extract key details from the title, description, fields, and comments
             * Pay special attention to comments as they often contain crucial information such as:
               - Additional context about the problem
               - Links to upstream fixes or patches
               - Clarifications from reporters or developers
             * Look for keywords indicating the root cause of the problem
             * Identify specific error messages, log snippets, or CVE identifiers
             * Note any functions, files, or methods mentioned
             * Pay attention to any direct links to fixes provided in the issue

          2. Identify the package name that must be updated:
             * Determine the name of the package from the issue details (usually component name)
             * Confirm the package repository exists by running
               `git ls-remote https://gitlab.com/redhat/centos-stream/rpms/<package_name>`
             * A successful command (exit code 0) confirms the package exists
             * If the package does not exist, re-examine the Jira issue for the correct package name and if it is not found,
               return error and explicitly state the reason

          3. Identify the target branch for updates:
             * Look at the fixVersion field in the Jira issue to determine the target branch
             * Apply the mapping rule: fixVersion named rhel-N maps to branch named cNs
             * Verify the branch exists on GitLab
             * This branch information will be needed for both rebases and backports

          4. Proceed to decision making process described below.

          **Decision Guidelines & Investigation Steps**

          You must decide between one of 5 actions. Follow these guidelines to make your decision:

          1. **Rebase**
             * A Rebase is only to be chosen when the issue explicitly instructs you to "rebase" or "update"
               to a newer/specific upstream version. Do not infer this.
             * Identify the <package_version> the package should be updated or rebased to.

          2. **Backport a Patch OR Request Clarification**
             This path is for issues that represent a clear bug or CVE that needs a targeted fix.

             2.1. Deep Analysis of the Issue
             * Use the details extracted from your initial analysis
             * Focus on keywords and root cause identification
             * If the Jira issue already provides a direct link to the fix, use that as your primary lead
               (e.g. in the commit hash field or comment)

             2.2. Systematic Source Investigation
             * Identify the official upstream project and corresponding Fedora package source
             * Even if the Jira issue provides a direct link to a fix, you need to validate it
             * When no direct link is provided, you must proactively search for fixes - do not give up easily
             * Using the details from your analysis, search these sources:
               - Bug Trackers (for fixed bugs matching the issue title and description)
               - Git / Version Control (for commit messages, using keywords, CVE IDs, function names, etc.)
             * Be thorough in your search - try multiple search terms and approaches based on the issue details
             * Advanced investigation techniques:
               - If you can identify specific files, functions, or code sections mentioned in the issue,
                 locate them in the source code
               - Use git history (git log, git blame) to examine changes to those specific code areas
               - Look for commits that modify the problematic code, especially those with relevant keywords in commit messages
               - Check git tags and releases around the time when the issue was likely fixed
               - Search for commits by date ranges when you know approximately when the issue was resolved
               - Utilize dates strategically in your search if needed, using the version/release date of the package
                 currently used in RHEL
                 - Focus on fixes that came after the RHEL package version date, as earlier fixes would already be included
                 - For CVEs, use the CVE publication date to narrow down the timeframe for fixes
                 - Check upstream release notes and changelogs after the RHEL package version date

             2.3. Validate the Fix and URL
             * Use the PatchValidator tool to fetch content from any patch/commit URL you intend to use
             * The tool will verify the URL is accessible and not an issue reference, then return the content
             * Once you have the content, you must validate two things:
               1. **Is it a patch/diff?** Look for diff indicators like:
                  - `diff --git` headers
                  - `--- a/file +++ b/file` unified diff headers
                  - `@@...@@` hunk headers
                  - `+` and `-` lines showing changes
               2. **Does it fix the issue?** Examine the actual code changes to verify:
                  - The fix directly addresses the root cause identified in your analysis
                  - The code changes align with the symptoms described in the Jira issue
                  - The modified functions/files match those mentioned in the issue
             * Only proceed with URLs that contain valid patch content AND address the specific issue
             * If the content is not a proper patch or doesn't fix the issue, continue searching for other fixes

             2.4. Decide the Outcome
             * If your investigation successfully identifies a specific fix that passes both validations in step 2.3, your decision is backport
             * You must be able to justify why the patch is correct and how it addresses the issue
             * If your investigation confirms a valid bug/CVE but fails to locate a specific fix, your decision
               is clarification-needed
             * This is the correct choice when you are sure a problem exists but cannot find the solution yourself

          3. **No Action**
             A No Action decision is appropriate for issues that are NOT bugs or CVEs requiring code fixes:
             * QE tasks, testing, or validation work
             * Feature requests or enhancements
             * Refactoring or code restructuring without fixing bugs
             * Documentation, build system, or process changes
             * Vague requests or insufficient information to identify a bug
             * Note: This is not for valid bugs where you simply can't find the patch

          4. **Error**
             An Error decision is appropriate when there are processing issues that prevent proper analysis, e.g.:
             * The package mentioned in the issue cannot be found or identified
             * The issue cannot be accessed

          **Output Format**

          Your output must strictly follow the format below.

          JIRA_ISSUE: {{ issue }}
          DECISION: rebase | backport | clarification-needed | no-action | error

          If Rebase:
              PACKAGE: [package name]
              VERSION: [target version]
              BRANCH: [target branch]

          If Backport:
              PACKAGE: [package name]
              BRANCH: [target branch]
              PATCH_URL: [URL or reference to the source of the fix]
              JUSTIFICATION: [A brief but clear explanation of why this patch fixes the issue, linking it to the root cause.]

          If Clarification Needed:
              FINDINGS: [Summarize your understanding of the bug and what you investigated,
                e.g., "The CVE-2025-XXXX describes a buffer overflow in the parse_input() function.
                I have scanned the upstream and Fedora git history for related commits but could not find a definitive fix."]
              ADDITIONAL_INFO_NEEDED: [State what information you are missing. e.g., "A link to the upstream commit
                that fixes this issue, or a patch file, is required to proceed."]

          If Error:
              DETAILS: [Provide specific details about the error. e.g., "Package 'invalid-package-name' not found
                in GitLab repository after examining issue details."]

          If No Action:
              REASONING: [Provide a concise reason why the issue is intentionally non-actionable,
                e.g., "The request is for a new feature ('add dark mode') which is not appropriate for a bugfix update in RHEL."]
        """

    async def run_with_schema(self, input: TInputSchema) -> TOutputSchema:
        async with mcp_tools(
            os.getenv("MCP_GATEWAY_URL"), filter=lambda t: t == "get_jira_details"
        ) as gateway_tools:
            tools = self._tools.copy()
            try:
                self._tools.extend(gateway_tools)
                return await self._run_with_schema(input)
            finally:
                self._tools = tools
                # disassociate removed tools from requirements
                for requirement in self._requirements:
                    if requirement._source_tool in gateway_tools:
                        requirement._source_tool = None


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
    agent = TriageAgent()

    if jira_issue := os.getenv("JIRA_ISSUE", None):
        logger.info("Running in direct mode with environment variable")
        input = InputSchema(issue=jira_issue)
        output = await agent.run_with_schema(input)
        logger.info(f"Direct run completed: {output.model_dump_json(indent=4)}")
        return

    class Task(BaseModel):
        metadata: dict = Field(description="Task metadata")
        attempts: int = Field(default=0, description="Number of processing attempts")

    logger.info("Starting triage agent in queue mode")
    async with redis_client(os.getenv("REDIS_URL")) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        logger.info(f"Connected to Redis, max retries set to {max_retries}")

        while True:
            logger.info("Waiting for tasks from triage_queue (timeout: 30s)...")
            element = await redis.brpop("triage_queue", timeout=30)
            if element is None:
                logger.info("No tasks received, continuing to wait...")
                continue

            _, payload = element
            logger.info(f"Received task from queue")

            task = Task.model_validate_json(payload)
            input = InputSchema.model_validate(task.metadata)
            logger.info(f"Processing triage for JIRA issue: {input.issue}, "
                       f"attempt: {task.attempts + 1}")

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(f"Task failed (attempt {task.attempts}/{max_retries}), "
                                 f"re-queuing for retry: {input.issue}")
                    await redis.lpush("triage_queue", task.model_dump_json())
                else:
                    logger.error(f"Task failed after {max_retries} attempts, "
                               f"moving to error list: {input.issue}")
                    await redis.lpush("error_list", error)

            try:
                logger.info(f"Starting triage processing for {input.issue}")
                output = await agent.run_with_schema(input)
                logger.info(f"Triage processing completed for {input.issue}, "
                          f"resolution: {output.resolution.value}")
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during triage processing for {input.issue}: {error}")
                await retry(
                    task, ErrorData(details=error, jira_issue=input.issue).model_dump_json()
                )
            else:
                if output.resolution == Resolution.REBASE:
                    logger.info(f"Triage resolved as REBASE for {input.issue}, "
                              f"adding to rebase queue")
                    task = Task(metadata=output.data.model_dump())
                    await redis.lpush("rebase_queue", task.model_dump_json())
                elif output.resolution == Resolution.BACKPORT:
                    logger.info(f"Triage resolved as BACKPORT for {input.issue}, "
                              f"adding to backport queue")
                    task = Task(metadata=output.data.model_dump())
                    await redis.lpush("backport_queue", task.model_dump_json())
                elif output.resolution == Resolution.CLARIFICATION_NEEDED:
                    logger.info(f"Triage resolved as CLARIFICATION_NEEDED for {input.issue}, "
                              f"adding to clarification needed queue")
                    task = Task(metadata=output.data.model_dump())
                    await redis.lpush("clarification_needed_queue", task.model_dump_json())
                elif output.resolution == Resolution.NO_ACTION:
                    logger.info(f"Triage resolved as NO_ACTION for {input.issue}, "
                              f"adding to no action list")
                    await redis.lpush("no_action_list", output.data.model_dump_json())
                elif output.resolution == Resolution.ERROR:
                    logger.warning(f"Triage resolved as ERROR for {input.issue}, retrying")
                    await retry(task, output.data.model_dump_json())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
