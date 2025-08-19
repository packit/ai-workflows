import asyncio
import json
import logging
import os
import re
import sys
import traceback
from enum import Enum
from typing import Union

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental import RequirementAgent
from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.backend import ChatModel
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.template import PromptTemplate, PromptTemplateInput
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow

from observability import setup_observability
from tools.commands import RunShellCommandTool
from tools.patch_validator import PatchValidatorTool
from tools.version_mapper import VersionMapperTool
from utils import fix_await, get_agent_execution_config, mcp_tools, redis_client, post_private_jira_comment, run_tool

logger = logging.getLogger(__name__)


def determine_target_branch(cve_eligibility_result: dict | None, triage_data) -> str | None:
    """
    Determine target branch from fix_version and CVE eligibility.
    """
    if not (hasattr(triage_data, 'fix_version') and triage_data.fix_version):
        logger.warning("No fix_version available for branch mapping")
        return None

    # Check if CVE needs internal fix first
    needs_internal_fix = (
        cve_eligibility_result
        and cve_eligibility_result.get("is_cve")
        and cve_eligibility_result.get("needs_internal_fix", False)
    )

    return _map_version_to_branch(triage_data.fix_version, needs_internal_fix)


def _map_version_to_branch(version: str, needs_internal_fix: bool) -> str | None:
    """
    Map version string to target branch.

    Args:
        version: Version string like 'rhel-9.8' or 'rhel-10.2'
        needs_internal_fix: True if fix in internal RHEL is needed first

    Returns:
        - RHEL internal fix: rhel-{major}.{minor}.0 (for RHEL 10, without .0 suffix)
        - CentOS Stream: c{major}s
    """
    version_match = re.match(r"^rhel-(\d+)\.(\d+)", version.lower())
    if not version_match:
        logger.warning(f"Failed to parse version: {version}")
        return None

    major_version = version_match.group(1)
    minor_version = version_match.group(2)

    if needs_internal_fix:
        branch = f"rhel-{major_version}.{minor_version}"
        if major_version != "10":
            branch += ".0"
        logger.info(f"Mapped {version} -> {branch} (RHEL internal fix)")
    else:
        branch = f"c{major_version}s"
        logger.info(f"Mapped {version} -> {branch} (CentOS Stream)")

    return branch


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
    version: str = Field(description="Target upstream package version (e.g., '2.4.1')")
    jira_issue: str = Field(description="Jira issue identifier")
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class BackportData(BaseModel):
    package: str = Field(description="Package name")
    patch_url: str = Field(description="URL or reference to the source of the fix")
    justification: str = Field(description="Clear explanation of why this patch fixes the issue")
    jira_issue: str = Field(description="Jira issue identifier")
    cve_id: str = Field(description="CVE identifier")
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


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


def render_prompt(input: InputSchema) -> str:
    template = """
      You are an agent tasked to analyze Jira issues for RHEL and identify the most efficient path to resolution,
      whether through a version rebase, a patch backport, or by requesting clarification when blocked.

      **Important**: Focus on bugs, CVEs, and technical defects that need code fixes.
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
         * Set the Jira fields as per the instructions below.

      2. **Backport a Patch OR Request Clarification**
         This path is for issues that represent a clear bug or CVE that needs a targeted fix.

         2.1. Deep Analysis of the Issue
         * Use the details extracted from your initial analysis
         * Focus on keywords and root cause identification
         * If the Jira issue already provides a direct link to the fix, use that as your primary lead
           (e.g. in the commit hash field or comment)

         2.2. Systematic Source Investigation
         * Identify the official upstream project from two sources:
            * Links from the Jira issue (if any direct upstream links are provided)
            * Package spec file (<package>.spec) in the GitLab repository: check the URL field or Source0 field for upstream project location

         * Even if the Jira issue provides a direct link to a fix, you need to validate it
         * When no direct link is provided, you must proactively search for fixes - do not give up easily
         * Using the details from your analysis, search these sources:
           - Bug Trackers (for fixed bugs matching the issue summary and description)
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

         2.5 Set the Jira fields as per the instructions below.

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

      **Final Step: Set JIRA Fields (for Rebase and Backport decisions only)**

         If your decision is rebase or backport, use set_jira_fields tool to update JIRA fields (Severity, Fix Version):
         1. Check all of the mentioned fields in the JIRA issue and don't modify those that are already set
         2. Extract the affected RHEL major version from the JIRA issue (look in Affects Version/s field or issue description)
         3. Determine if this is a very critical issue requiring Z-stream (only for: privilege escalation, remote code execution, data loss/corruption, or system compromise)
         4. Use map_version tool with the major version and criticality to get the appropriate Fix Version
         5. Set JIRA fields:
             * Severity: default to 'moderate', for important issues use 'important', for most critical use 'critical' (privilege escalation, RCE, data loss)
             * Fix Version: use the fix_version from map_version tool result

      **Output Format**

      Your output must strictly follow the format below.

      JIRA_ISSUE: {{ issue }}
      DECISION: rebase | backport | clarification-needed | no-action | error

      If Rebase:
          PACKAGE: [package name]
          VERSION: [target version]
          FIX_VERSION: [fix version set in JIRA]

      If Backport:
          PACKAGE: [package name]
          PATCH_URL: [URL or reference to the source of the fix]
          CVE_ID: [CVE identifier, leave blank if not applicable]
          JUSTIFICATION: [A brief but clear explanation of why this patch fixes the issue, linking it to the root cause.]
          FIX_VERSION: [fix version set in JIRA]

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
    return PromptTemplate(PromptTemplateInput(schema=InputSchema, template=template)).render(input)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        triage_agent = RequirementAgent(
            llm=ChatModel.from_name(os.getenv("CHAT_MODEL")),
            tools=[ThinkTool(), RunShellCommandTool(), PatchValidatorTool(), VersionMapperTool()]
            + [t for t in gateway_tools if t.name in ["get_jira_details", "set_jira_fields"]],
            memory=UnconstrainedMemory(),
            requirements=[
                ConditionalRequirement(ThinkTool, force_after=Tool, consecutive_allowed=False),
                ConditionalRequirement("get_jira_details", min_invocations=1),
                ConditionalRequirement(RunShellCommandTool, only_after="get_jira_details"),
                ConditionalRequirement(PatchValidatorTool, only_after="get_jira_details"),
                ConditionalRequirement("set_jira_fields", only_after="get_jira_details"),
            ],
            middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
            role="Red Hat Enterprise Linux developer",
            instructions=[
                "Use the `think` tool to reason through complex decisions and document your approach.",
                "Be proactive in your search for fixes and do not give up easily.",
                "After completing your triage analysis, if your decision is backport or rebase, always set appropriate JIRA fields per the instructions using set_jira_fields tool.",
            ]
        )

        class State(BaseModel):
            jira_issue: str
            cve_eligibility_result: dict | None = Field(default=None)
            triage_result: OutputSchema | None = Field(default=None)
            target_branch: str | None = Field(default=None)

        workflow = Workflow(State)

        async def check_cve_eligibility(state):
            """Check CVE eligibility for the issue"""
            logger.info(f"Checking CVE eligibility for {state.jira_issue}")
            result = await run_tool(
                "check_cve_triage_eligibility",
                available_tools=gateway_tools,
                issue_key=state.jira_issue
            )
            state.cve_eligibility_result = json.loads(result)

            logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")

            # If not eligible for triage, end workflow
            if not state.cve_eligibility_result.get("is_eligible_for_triage", True):
                reason = state.cve_eligibility_result.get('reason', 'Not eligible for triage')
                logger.info(f"Issue {state.jira_issue} not eligible for triage: {reason}")
                state.triage_result = OutputSchema(
                    resolution=Resolution.NO_ACTION,
                    data=NoActionData(
                        reasoning=f"CVE eligibility check: {reason}",
                        jira_issue=state.jira_issue
                    )
                )
                return Workflow.END

            reason = state.cve_eligibility_result.get('reason', 'Eligible')
            logger.info(f"Issue {state.jira_issue} is eligible for triage: {reason}")
            return "run_triage_analysis"

        async def run_triage_analysis(state):
            """Run the main triage analysis"""
            logger.info(f"Running triage analysis for {state.jira_issue}")
            input_data = InputSchema(issue=state.jira_issue)
            response = await triage_agent.run(
                prompt=render_prompt(input_data),
                expected_output=OutputSchema,
                execution=get_agent_execution_config(),
            )
            state.triage_result = OutputSchema.model_validate_json(response.answer.text)

            if state.triage_result.resolution in [Resolution.REBASE, Resolution.BACKPORT]:
                return "determine_target_branch"
            else:
                return Workflow.END

        async def determine_target_branch_step(state):
            """Determine target branch for rebase/backport decisions"""
            logger.info(f"Determining target branch for {state.jira_issue}")

            state.target_branch = determine_target_branch(
                cve_eligibility_result=state.cve_eligibility_result,
                triage_data=state.triage_result.data
            )

            if state.target_branch:
                logger.info(f"Target branch determined: {state.target_branch}")
            else:
                logger.warning(f"Could not determine target branch for {state.jira_issue}")

            return Workflow.END

        workflow.add_step("check_cve_eligibility", check_cve_eligibility)
        workflow.add_step("run_triage_analysis", run_triage_analysis)
        workflow.add_step("determine_target_branch", determine_target_branch_step)

        async def run_workflow(jira_issue):
            response = await workflow.run(State(jira_issue=jira_issue))
            return response.state

        if jira_issue := os.getenv("JIRA_ISSUE", None):
            logger.info("Running in direct mode with environment variable")
            state = await run_workflow(jira_issue)
            logger.info(f"Direct run completed: {state.triage_result.model_dump_json(indent=4)}")
            if state.cve_eligibility_result:
                logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")
            if state.target_branch:
                logger.info(f"Target branch: {state.target_branch}")
            return

        class Task(BaseModel):
            metadata: dict = Field(description="Task metadata")
            attempts: int = Field(default=0, description="Number of processing attempts")

        logger.info("Starting triage agent in queue mode")
        async with redis_client(os.environ["REDIS_URL"]) as redis:
            max_retries = int(os.getenv("MAX_RETRIES", 3))
            logger.info(f"Connected to Redis, max retries set to {max_retries}")

            while True:
                logger.info("Waiting for tasks from triage_queue (timeout: 30s)...")
                element = await fix_await(redis.brpop(["triage_queue"], timeout=30))
                if element is None:
                    logger.info("No tasks received, continuing to wait...")
                    continue

                _, payload = element
                logger.info("Received task from queue")

                task = Task.model_validate_json(payload)
                input = InputSchema.model_validate(task.metadata)
                logger.info(f"Processing triage for JIRA issue: {input.issue}, " f"attempt: {task.attempts + 1}")

                async def retry(task, error):
                    task.attempts += 1
                    if task.attempts < max_retries:
                        logger.warning(
                            f"Task failed (attempt {task.attempts}/{max_retries}), "
                            f"re-queuing for retry: {input.issue}"
                        )
                        await fix_await(redis.lpush("triage_queue", task.model_dump_json()))
                    else:
                        logger.error(
                            f"Task failed after {max_retries} attempts, " f"moving to error list: {input.issue}"
                        )
                        await fix_await(redis.lpush("error_list", error))

                try:
                    logger.info(f"Starting triage processing for {input.issue}")
                    state = await run_workflow(input.issue)
                    output = state.triage_result
                    logger.info(
                        f"Triage processing completed for {input.issue}, " f"resolution: {output.resolution.value}"
                    )
                    if state.cve_eligibility_result:
                        logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")
                    if state.target_branch:
                        logger.info(f"Target branch: {state.target_branch}")

                    agent_type = "Triage"
                    if output.resolution.value == "clarification-needed":
                        await post_private_jira_comment(gateway_tools, input.issue, agent_type, output.data.additional_info_needed)
                    elif output.resolution.value == "no-action":
                        await post_private_jira_comment(gateway_tools, input.issue, agent_type, output.data.reasoning)
                        
                except Exception as e:
                    error = "".join(traceback.format_exception(e))
                    logger.error(f"Exception during triage processing for {input.issue}: {error}")
                    await retry(task, ErrorData(details=error, jira_issue=input.issue).model_dump_json())
                else:
                    if output.resolution == Resolution.REBASE:
                        logger.info(f"Triage resolved as REBASE for {input.issue}, " f"adding to rebase queue")
                        task = Task(metadata=state.model_dump())
                        await redis.lpush("rebase_queue", task.model_dump_json())
                    elif output.resolution == Resolution.BACKPORT:
                        logger.info(f"Triage resolved as BACKPORT for {input.issue}, " f"adding to backport queue")
                        task = Task(metadata=state.model_dump())
                        await redis.lpush("backport_queue", task.model_dump_json())
                    elif output.resolution == Resolution.CLARIFICATION_NEEDED:
                        logger.info(
                            f"Triage resolved as CLARIFICATION_NEEDED for {input.issue}, "
                            f"adding to clarification needed queue"
                        )
                        task = Task(metadata=state.model_dump())
                        await redis.lpush("clarification_needed_queue", task.model_dump_json())
                    elif output.resolution == Resolution.NO_ACTION:
                        logger.info(f"Triage resolved as NO_ACTION for {input.issue}, " f"adding to no action list")
                        await fix_await(redis.lpush("no_action_list", output.data.model_dump_json()))
                    elif output.resolution == Resolution.ERROR:
                        logger.warning(f"Triage resolved as ERROR for {input.issue}, retrying")
                        await retry(task, output.data.model_dump_json())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
