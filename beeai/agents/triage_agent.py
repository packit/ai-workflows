import asyncio
import os
import sys
import traceback
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental import RequirementAgent
from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.template import PromptTemplate, PromptTemplateInput
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool

from gemini import GeminiChatModel
from models import RebaseTask
from observability import setup_observability
from tools import ShellCommandTool
from utils import mcp_tools, redis_client


class PromptSchema(BaseModel):
    issue: str = Field(description="Jira issue identifier to analyze (e.g. RHEL-12345)")


class ResolutionEnum(Enum):
    REBASE = "rebase"
    BACKPORT = "backport"
    CLARIFICATION_NEEDED = "clarification-needed"
    NO_ACTION = "no-action"
    ERROR = "error"


class TriageOutput(BaseModel):
    resolution: ResolutionEnum = Field(description="Triage resolution")
    package: Optional[str] = Field(description="Package name")
    version: Optional[str] = Field(description="Target version")
    branch: Optional[str] = Field(description="Target branch")
    patch_url: Optional[str] = Field(description="URL or reference to the source of the fix")
    justification: Optional[str] = Field(description="Clear explanation of why this patch fixes the issue")
    findings: Optional[str] = Field(description="Summary of the investigation")
    additional_info_needed: Optional[str] = Field(description="Summary of missing information")
    details: Optional[str] = Field(description="Specific details about an error")
    reasoning: Optional[str] = Field(description="Reason why the issue is intentionally non-actionable")


prompt_template: PromptTemplate[PromptSchema] = PromptTemplate(
    PromptTemplateInput(
        schema=PromptSchema,
        template="""
          You are an agent tasked to analyze Jira issues for RHEL and identify the most efficient path to resolution,
          whether through a version rebase, a patch backport, or by requesting clarification when blocked.

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
               - Bug Trackers (for fixed bugs matching the issue description)
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

             2.3. Validate the Fix
             * When you think you've found a potential fix, examine the actual content of the patch/commit
             * Verify that the fix directly addresses the root cause identified in your analysis
             * Check if the code changes align with the symptoms described in the Jira issue
             * If the fix doesn't appear to resolve the specific issue, continue searching for other fixes
             * Don't settle for the first fix you find - ensure it's the right one

             2.4. Validate the Fix URL
             * Make sure to provide a valid URL to the patch/commit
             * If the URL is not valid, re-do previous steps

             2.5. Decide the Outcome
             * If your investigation successfully identifies a specific fix that you've validated, your decision is backport
             * You must be able to justify why the patch is correct and how it addresses the issue
             * If your investigation confirms a valid bug/CVE but fails to locate a specific fix, your decision
               is clarification-needed
             * This is the correct choice when you are sure a problem exists but cannot find the solution yourself

          3. **No Action**
             A No Action decision is appropriate for issues that are intentionally non-actionable:
             * The request is too vague to be understood
             * It's a feature request
             * There is insufficient information to even begin an investigation
             * Note: This is not for valid bugs where you simply can't find the patch

          4. **Error**
             An Error decision is appropriate when there are processing issues that prevent proper analysis, e.g.:
             * The package mentioned in the issue cannot be found or identified
             * The issue cannot be accessed

          **Output Format**

          Your output must strictly follow the format below.

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
        """,
    )
)


async def main() -> None:
    async with mcp_tools(os.getenv("MCP_JIRA_URL"), filter=lambda t: t == "jira_get_issue") as jira_tools:
        agent = RequirementAgent(
            llm=GeminiChatModel(),
            tools=[ThinkTool(), ShellCommandTool()] + jira_tools,
            memory=UnconstrainedMemory(),
            requirements=[
                ConditionalRequirement(ThinkTool, force_after=Tool, consecutive_allowed=False),
                ConditionalRequirement("jira_get_issue", min_invocations=1),
                ConditionalRequirement(ShellCommandTool, only_after="jira_get_issue"),
            ],
            middlewares=[GlobalTrajectoryMiddleware()],
        )
        issue = os.getenv("JIRA_ISSUE")
        response = await agent.run(
            prompt=prompt_template.render(issue=issue),
            expected_output=TriageOutput,
        )
        triage_output = TriageOutput.model_validate_json(response.result.text)
        print(triage_output.model_dump_json(indent=4))
        if triage_output.resolution == ResolutionEnum.REBASE:
            rebase_task = RebaseTask(
                package_name=triage_output.package,
                package_version=triage_output.version,
                git_branch=triage_output.branch,
                jira_issue=issue,
            )
            async with redis_client(os.getenv("REDIS_URL")) as redis:
                await redis.lpush("rebase_queue", rebase_task.model_dump_json())


if __name__ == "__main__":
    try:
        setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
