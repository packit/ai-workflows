import asyncio
import logging
import os
import re
import sys
import traceback
from textwrap import dedent

from pydantic import BaseModel, Field

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.template import PromptTemplate
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from beeai_framework.utils.strings import to_json

import agents.tasks as tasks
from common.config import load_rhel_config
from common.models import (
    Task,
    TriageInputSchema as InputSchema,
    TriageOutputSchema as OutputSchema,
    Resolution,
    ClarificationNeededData,
    NoActionData,
    ErrorData,
    CVEEligibilityResult,
)
from common.utils import redis_client, fix_await
from common.constants import JiraLabels, RedisQueues
from agents.observability import setup_observability
from agents.tools.commands import RunShellCommandTool
from agents.tools.patch_validator import PatchValidatorTool
from agents.tools.version_mapper import VersionMapperTool
from agents.tools.upstream_search import UpstreamSearchTool
from agents.utils import get_agent_execution_config, get_chat_model, get_tool_call_checker_config, mcp_tools, run_tool

logger = logging.getLogger(__name__)


async def determine_target_branch(cve_eligibility_result: CVEEligibilityResult | None, triage_data: BaseModel) -> str | None:
    """
    Determine target branch from fix_version and CVE eligibility.
    """
    if not (hasattr(triage_data, 'fix_version') and triage_data.fix_version):
        logger.warning("No fix_version available for branch mapping")
        return None

    # Check if CVE needs internal fix first
    cve_needs_internal_fix = (
        cve_eligibility_result
        and cve_eligibility_result.is_cve
        and cve_eligibility_result.needs_internal_fix
    )

    package = triage_data.package if hasattr(triage_data, 'package') else None

    return await _map_version_to_branch(triage_data.fix_version, cve_needs_internal_fix, package)


def _construct_internal_branch_name(major_version: str, minor_version: str) -> str:
    """Construct internal RHEL branch name."""
    branch = f"rhel-{major_version}.{minor_version}"
    if int(major_version) < 10:
        branch += ".0"
    return branch


async def _map_version_to_branch(version: str, cve_needs_internal_fix: bool, package: str | None = None) -> str | None:
    """
    Map version string to target branch.

    Args:
        version: Version string like 'rhel-9.8' or 'rhel-10.2.z'
        cve_needs_internal_fix: True if CVE fix in internal RHEL is needed first
        package: Package name for checking internal branches (required for Z-stream)

    Returns:
        - RHEL internal fix: rhel-{major}.{minor}.0 (for RHEL 10, without .0 suffix)
        - CentOS Stream: c{major}s
    """
    version_match = re.match(r"^rhel-(\d+)\.(\d+)(\.z)?", version.lower())
    if not version_match:
        logger.warning(f"Failed to parse version: {version}")
        return None

    major_version = version_match.group(1)
    minor_version = version_match.group(2)
    is_zstream = version_match.group(3) is not None

    # Load rhel-config to check which major versions have Y-stream mappings
    config = await load_rhel_config()
    y_streams = config.get("current_y_streams", {})


    if cve_needs_internal_fix:
        if major_version in y_streams:
            branch = _construct_internal_branch_name(major_version, minor_version)
            logger.info(f"Mapped {version} -> {branch} (CVE internal fix)")
            return branch
        # Default to CentOS Stream for CVEs when no Y-stream
        branch = f"c{major_version}s"
        logger.info(f"Mapped {version} -> {branch} (CentOS Stream)")
        return branch

    # For Z-stream bugs, check if internal RHEL branch exists
    if is_zstream and package:
        expected_branch = _construct_internal_branch_name(major_version, minor_version)

        try:
            async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
                available_branches = await run_tool(
                    "get_internal_rhel_branches",
                    available_tools=gateway_tools,
                    package=package
                )

                if expected_branch in available_branches:
                    logger.info(f"Mapped {version} -> {expected_branch} (Z-stream with internal branch)")
                    return expected_branch
                logger.info(f"Internal branch {expected_branch} not found for package {package}, falling back to Stream")
        except Exception as e:
            logger.warning(f"Failed to check internal branches for package {package}: {e}, falling back to Stream")

    # Default to CentOS Stream
    branch = f"c{major_version}s"
    logger.info(f"Mapped {version} -> {branch} (CentOS Stream)")
    return branch


# All schemas are now imported from common.models


def render_prompt(input: InputSchema) -> str:
    template = """
      You are an agent tasked to analyze Jira issues for RHEL and identify the most efficient path to resolution,
      whether through a version rebase, a patch backport, or by requesting clarification when blocked.

      **Important**: Focus on bugs, CVEs, and technical defects that need code fixes.
      QE tasks, feature requests, refactoring, documentation, and other non-bug issues should be marked as "no-action".

      Goal: Analyze the given issue to determine the correct course of action.

      **Initial Analysis Steps**

      1. Open the {{issue}} Jira issue and thoroughly analyze it:
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
           `GIT_TERMINAL_PROMPT=0 git ls-remote https://gitlab.com/redhat/centos-stream/rpms/<package_name>`
         * A successful command (exit code 0) confirms the package exists
         * If the package does not exist, re-examine the Jira issue for the correct package name and if it is not found,
           return error and explicitly state the reason

      3. Proceed to decision making process described below.

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
         * Even if the Jira issue provides a direct link to a fix, you need to validate it
         * When no direct link is provided, you must proactively search for fixes - do not give up easily
         * There are 2 locations where you can search for the fixes: Fedora and upstream project.
         * First, check if the fix is in Fedora repository in https://src.fedoraproject.org/rpms/<package_name>.
           * In Fedora, search for .patch files and check git commit history for fixes using relevant keywords (CVE IDs, function names, error messages)
         * If it's not, identify the official upstream project from the following 2 sources and search there:
            * Links from the Jira issue (if any direct upstream links are provided)
            * Package spec file (<package>.spec) in the GitLab repository: check the URL field or Source0 field for upstream project location

         * Try to use upstream_search tool to find out commits related to the issue.
           - The description you will use should be 1-2 sentences long and include implementation
             details, keywords, function names or any other helpful information.
           - The description should be like a command for example `Fix`, `Add` etc.
           - If the tool gives you list of URLs use them without any change.
           - Use release date of upstream version used in RHEL if you know it.
           - If the tool says it can not be used for this project, or it encounters internal error,
             do not try to use it again and proceed with different approach.
           - If you run out of commits to check, use different approach, do not give up. Inability
             of the tool to find proper fix does not mean it does not exist, search bug trackers
             and version control system.
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
         3. If the Fix Version field is set, do not change it and use its value in the output.
         4. If the Fix Version field is not set, use the map_version tool with the major version to get available streams
            and determine appropriate Fix Version:
             * The tool will return both Y-stream and Z-stream versions (if available) and indicate if it's a maintenance version
             * For maintenance versions (no Y-stream available):
               - Critical issues should be fixed (privilege escalation, remote code execution, data loss/corruption, system compromise, regressions, moderate and higher severity CVEs)
               - Non-critical issues should be marked as no-action with appropriate reasoning
             * For non-maintenance versions (Y-stream available):
               - Most critical issues (privilege escalation, RCE, data loss, regressions) should use Z-stream
               - Other issues should use Y-stream (e.g. performance, usability issues)
         5. Set non-empty JIRA fields:
             * Severity: default to 'moderate', for important issues use 'important', for most critical use 'critical' (privilege escalation, RCE, data loss)
             * Fix Version: use the appropriate stream version determined from map_version tool result
    """
    return PromptTemplate(schema=InputSchema, template=template).render(input)


class TriageState(BaseModel):
    jira_issue: str
    cve_eligibility_result: CVEEligibilityResult | None = Field(default=None)
    triage_result: OutputSchema | None = Field(default=None)
    target_branch: str | None = Field(default=None)


def create_triage_agent(gateway_tools):
    return RequirementAgent(
        name="TriageAgent",
        llm=get_chat_model(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[ThinkTool(), RunShellCommandTool(), PatchValidatorTool(),
                VersionMapperTool(), UpstreamSearchTool()]
        + [t for t in gateway_tools if t.name in ["get_jira_details", "set_jira_fields"]],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                force_after=Tool,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
            ConditionalRequirement("get_jira_details", min_invocations=1),
            ConditionalRequirement(UpstreamSearchTool, only_after="get_jira_details"),
            ConditionalRequirement(RunShellCommandTool, only_after="get_jira_details"),
            ConditionalRequirement(PatchValidatorTool, only_after="get_jira_details"),
            ConditionalRequirement("set_jira_fields", only_after="get_jira_details"),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat Enterprise Linux developer",
        instructions=[
            "Use the `think` tool to reason through complex decisions and document your approach.",
            "Be proactive in your search for fixes and do not give up easily.",
            "For any patch URL that you are proposing for backport, you need to validate it using PatchValidator tool.",
            "Do not modify the patch URL in your final answer after it has been validated with the PatchValidator tool.",
            "After completing your triage analysis, if your decision is backport or rebase, always set appropriate JIRA fields per the instructions using set_jira_fields tool.",
        ]
    )


async def run_workflow(jira_issue, dry_run, triage_agent_factory):
    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        triage_agent = triage_agent_factory(gateway_tools)

        workflow = Workflow(TriageState, name="TriageWorkflow")

        async def check_cve_eligibility(state):
            """Check CVE eligibility for the issue"""
            logger.info(f"Checking CVE eligibility for {state.jira_issue}")
            result = await run_tool(
                "check_cve_triage_eligibility",
                available_tools=gateway_tools,
                issue_key=state.jira_issue
            )
            state.cve_eligibility_result = CVEEligibilityResult.model_validate(result)

            logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")

            # If not eligible for triage, end workflow
            if not state.cve_eligibility_result.is_eligible_for_triage:
                logger.info(f"Issue {state.jira_issue} not eligible for triage: {state.cve_eligibility_result.reason}")
                if state.cve_eligibility_result.error:
                    state.triage_result = OutputSchema(
                    resolution=Resolution.ERROR,
                    data=ErrorData(
                        details=f"CVE eligibility check error: {state.cve_eligibility_result.error}",
                        jira_issue=state.jira_issue
                    )
                )
                else:
                    state.triage_result = OutputSchema(
                        resolution=Resolution.NO_ACTION,
                        data=NoActionData(
                            reasoning=f"CVE eligibility check decided to skip triaging: {state.cve_eligibility_result.reason}",
                            jira_issue=state.jira_issue
                        )
                    )
                return "comment_in_jira"

            reason = state.cve_eligibility_result.reason
            logger.info(f"Issue {state.jira_issue} is eligible for triage: {reason}")
            return "run_triage_analysis"

        async def run_triage_analysis(state):
            """Run the main triage analysis"""
            logger.info(f"Running triage analysis for {state.jira_issue}")
            input_data = InputSchema(issue=state.jira_issue)
            output_schema_json = to_json(
                OutputSchema.model_json_schema(mode="validation"),
                indent=2,
                sort_keys=False,
            )
            response = await triage_agent.run(
                render_prompt(input_data),
                # `OutputSchema` alone is not enough here, some models (cough cough, Claude Sonnet 4.5)
                # really stuggle with the nesting, let's provide some more hints
                expected_output=dedent(
                    f"""
                    The final answer must fulfill the following.

                    **Important Formatting Rules:**
                    - The top-level output must be a JSON object with two keys: `resolution` (a string) and `data` (an object).
                    - The `data` field MUST be a nested JSON object. **It must not be a stringified JSON object.**
                    - The structure of the `data` object must match the schema corresponding to the chosen `resolution`.

                    **Correct example for a 'backport' resolution:**
                    ```json
                    {{
                        "resolution": "backport",
                        "data": {{
                        "package": "some-package",
                        "patch_url": "https://example.com/some.patch",
                        "justification": "This patch fixes the bug by doing X, Y, and Z.",
                        "jira_issue": "RHEL-12345",
                        "cve_id": "CVE-1234-98765",
                        "fix_version": "rhel-X.Y.Z"
                        }}
                    }}
                    ```

                    ```json
                    {output_schema_json}
                    ```
                    """
                ),
                **get_agent_execution_config(),
            )
            state.triage_result = OutputSchema.model_validate_json(response.last_message.text)

            # Jira issue key in resolution data has been generated by LLM, make sure it's upper-case
            state.triage_result.data.jira_issue = state.triage_result.data.jira_issue.upper()

            if state.triage_result.resolution == Resolution.REBASE:
                return "verify_rebase_author"
            elif state.triage_result.resolution == Resolution.BACKPORT:
                return "determine_target_branch"
            elif state.triage_result.resolution in [Resolution.CLARIFICATION_NEEDED, Resolution.NO_ACTION]:
                return "comment_in_jira"
            else:
                return Workflow.END

        async def determine_target_branch_step(state):
            """Determine target branch for rebase/backport decisions"""
            logger.info(f"Determining target branch for {state.jira_issue}")

            state.target_branch = await determine_target_branch(
                cve_eligibility_result=state.cve_eligibility_result,
                triage_data=state.triage_result.data
            )

            if state.target_branch:
                logger.info(f"Target branch determined: {state.target_branch}")
            else:
                logger.warning(f"Could not determine target branch for {state.jira_issue}")

            return "comment_in_jira"

        async def verify_rebase_author(state):
            """Verify that the issue author is a Red Hat employee"""
            logger.info(f"Verifying issue author for {state.jira_issue}")

            is_rh_employee = await run_tool(
                "verify_issue_author",
                available_tools=gateway_tools,
                issue_key=state.jira_issue
            )

            issue_status = await run_tool(
                "get_jira_details",
                available_tools=gateway_tools,
                issue_key=state.jira_issue
            )
            issue_status = issue_status.get("fields", {}).get("status", {}).get("name")

            if not is_rh_employee and issue_status == "New":
                logger.warning(f"Issue author for {state.jira_issue} is not verified as RH employee - ending triage with clarification needed")

                # override triage result with clarification needed so that it gets reviewed by us
                state.triage_result = OutputSchema(
                    resolution=Resolution.CLARIFICATION_NEEDED,
                    data=ClarificationNeededData(
                        findings="The rebase resolution was determined, but author verification failed.",
                        additional_info_needed="Needs human review, as the issue author is not verified as a Red Hat employee.",
                        jira_issue=state.jira_issue
                    )
                )

                return "comment_in_jira"

            logger.info(f"Issue author for {state.jira_issue} verified as RH employee or issue is not in new status - proceeding with rebase")

            return "determine_target_branch"

        async def comment_in_jira(state):
            comment_text = state.triage_result.format_for_comment()
            logger.info(f"Result to be put in Jira comment: {comment_text}")
            if dry_run:
                return Workflow.END
            await tasks.comment_in_jira(
                jira_issue=state.jira_issue,
                agent_type="Triage",
                comment_text=comment_text,
                available_tools=gateway_tools,
            )
            return Workflow.END

        workflow.add_step("check_cve_eligibility", check_cve_eligibility)
        workflow.add_step("run_triage_analysis", run_triage_analysis)
        workflow.add_step("verify_rebase_author", verify_rebase_author)
        workflow.add_step("determine_target_branch", determine_target_branch_step)
        workflow.add_step("comment_in_jira", comment_in_jira)

        response = await workflow.run(TriageState(jira_issue=jira_issue))
        return response.state


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    if jira_issue := os.getenv("JIRA_ISSUE", None):
        logger.info("Running in direct mode with environment variable")
        state = await run_workflow(jira_issue, dry_run, create_triage_agent)
        logger.info(f"Direct run completed: {state.triage_result.model_dump_json(indent=4)}")
        if state.cve_eligibility_result:
            logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")
        if state.target_branch:
            logger.info(f"Target branch: {state.target_branch}")
        return

    logger.info("Starting triage agent in queue mode")
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        logger.info(f"Connected to Redis, max retries set to {max_retries}")

        while True:
            logger.info("Waiting for tasks from triage_queue (timeout: 30s)...")
            element = await fix_await(redis.brpop([RedisQueues.TRIAGE_QUEUE.value], timeout=30))
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
                    await fix_await(redis.lpush(RedisQueues.TRIAGE_QUEUE.value, task.model_dump_json()))
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, " f"moving to error list: {input.issue}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[JiraLabels.TRIAGE_ERRORED.value],
                        dry_run=dry_run
                    )
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            try:
                await tasks.set_jira_labels(
                    jira_issue=input.issue,
                    labels_to_remove=list(JiraLabels.all_labels()),
                    dry_run=dry_run
                )
                logger.info(f"Cleaned up existing labels for {input.issue}")

                logger.info(f"Starting triage processing for {input.issue}")
                state = await run_workflow(input.issue, dry_run, create_triage_agent)
                output = state.triage_result
                logger.info(
                    f"Triage processing completed for {input.issue}, " f"resolution: {output.resolution.value}"
                )
                if state.cve_eligibility_result:
                    logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")
                if state.target_branch:
                    logger.info(f"Target branch: {state.target_branch}")

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during triage processing for {input.issue}: {error}")
                await retry(task, ErrorData(details=error, jira_issue=input.issue).model_dump_json())
            else:
                if output.resolution == Resolution.REBASE:
                    logger.info(f"Triage resolved as REBASE for {input.issue}, " f"adding to rebase queue")
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[JiraLabels.REBASE_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    task = Task(metadata=state.model_dump())
                    rebase_queue = RedisQueues.get_rebase_queue_for_branch(state.target_branch)
                    await redis.lpush(rebase_queue, task.model_dump_json())
                elif output.resolution == Resolution.BACKPORT:
                    logger.info(f"Triage resolved as BACKPORT for {input.issue}, " f"adding to backport queue")
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[JiraLabels.BACKPORT_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    task = Task(metadata=state.model_dump())
                    backport_queue = RedisQueues.get_backport_queue_for_branch(state.target_branch)
                    await redis.lpush(backport_queue, task.model_dump_json())
                elif output.resolution == Resolution.CLARIFICATION_NEEDED:
                    logger.info(
                        f"Triage resolved as CLARIFICATION_NEEDED for {input.issue}, "
                        f"adding to clarification needed queue"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[JiraLabels.NEEDS_ATTENTION.value],
                        dry_run=dry_run
                    )
                    task = Task(metadata=state.model_dump())
                    await redis.lpush(RedisQueues.CLARIFICATION_NEEDED_QUEUE.value, task.model_dump_json())
                elif output.resolution == Resolution.NO_ACTION:
                    logger.info(f"Triage resolved as NO_ACTION for {input.issue}, " f"adding to no action list")
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[JiraLabels.NO_ACTION_NEEDED.value],
                        dry_run=dry_run
                    )
                    await fix_await(redis.lpush(RedisQueues.NO_ACTION_LIST.value, output.data.model_dump_json()))
                elif output.resolution == Resolution.ERROR:
                    logger.warning(f"Triage resolved as ERROR for {input.issue}, retrying")
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[JiraLabels.TRIAGE_ERRORED.value],
                        dry_run=dry_run
                    )
                    await retry(task, output.data.model_dump_json())


if __name__ == "__main__":
    try:
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
