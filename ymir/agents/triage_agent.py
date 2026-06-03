import asyncio
import logging
import os
import shutil
import sys
import traceback
from pathlib import Path
from textwrap import dedent

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.template import PromptTemplate
from beeai_framework.tools.think import ThinkTool
from beeai_framework.utils.strings import to_json
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field

import ymir.agents.tasks as tasks
from ymir.agents.cve_applicability_agent import build_applicability_prompt, create_applicability_agent
from ymir.agents.observability import setup_observability
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.rebuild_consolidation import find_rebuild_siblings
from ymir.agents.utils import (
    build_agent_factory_with_mock_repos,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    init_sentry,
    is_reasoning_enabled,
    mcp_tools,
    resolve_chat_model_override,
    run_tool,
)
from ymir.common.base_utils import fix_await, redis_client
from ymir.common.config import load_rhel_config
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.logging_setup import configure_logging
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    ApplicabilityResult,
    ClarificationNeededData,
    CVEEligibilityResult,
    ErrorData,
    NotAffectedData,
    OpenEndedAnalysisData,
    PostponedData,
    Resolution,
    Task,
    TriageEligibility,
)
from ymir.common.models import (
    TriageInputSchema as InputSchema,
)
from ymir.common.models import (
    TriageOutputSchema as OutputSchema,
)
from ymir.common.utils import get_latest_candidate_build
from ymir.common.version_utils import is_older_zstream, normalize_fix_version, parse_rhel_version
from ymir.tools.unprivileged.commands import RunShellCommandTool

## UpstreamSearchTool is currently unmaintained and disabled.
# from ymir.tools.unprivileged.upstream_search import UpstreamSearchTool
from ymir.tools.unprivileged.version_mapper import VersionMapperTool

logger = logging.getLogger(__name__)


def _should_update_jira(silent_run: bool, resolution: Resolution = None) -> bool:
    """In silent mode, only update Jira for not-affected and postponed resolutions."""
    if not silent_run:
        return True
    return resolution in (Resolution.NOT_AFFECTED, Resolution.POSTPONED)


_RESOLUTION_TO_LABEL: dict[Resolution, JiraLabels] = {
    Resolution.REBASE: JiraLabels.TRIAGED_REBASE,
    Resolution.BACKPORT: JiraLabels.TRIAGED_BACKPORT,
    Resolution.REBUILD: JiraLabels.TRIAGED_REBUILD,
    Resolution.CLARIFICATION_NEEDED: JiraLabels.NEEDS_ATTENTION,
    Resolution.OPEN_ENDED_ANALYSIS: JiraLabels.TRIAGED,
    Resolution.POSTPONED: JiraLabels.TRIAGED_POSTPONED,
    Resolution.NOT_AFFECTED: JiraLabels.TRIAGED_NOT_AFFECTED,
    Resolution.ERROR: JiraLabels.TRIAGE_ERRORED,
}


async def determine_target_branch(
    cve_eligibility_result: CVEEligibilityResult | None, triage_data: BaseModel
) -> str | None:
    """
    Determine target branch from fix_version and CVE eligibility.
    """
    if not (hasattr(triage_data, "fix_version") and triage_data.fix_version):
        logger.warning("No fix_version available for branch mapping")
        return None

    # Check if CVE needs internal fix first
    cve_needs_internal_fix = (
        cve_eligibility_result and cve_eligibility_result.is_cve and cve_eligibility_result.needs_internal_fix
    )

    package = triage_data.package if hasattr(triage_data, "package") else None

    return await _map_version_to_branch(triage_data.fix_version, cve_needs_internal_fix, package)


def _construct_internal_branch_name(major_version: str, minor_version: str) -> str:
    """Construct internal RHEL branch name."""
    branch = f"rhel-{major_version}.{minor_version}"
    if int(major_version) < 10:
        branch += ".0"
    return branch


async def _map_version_to_branch(
    version: str, cve_needs_internal_fix: bool, package: str | None = None
) -> str | None:
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
    parsed = parse_rhel_version(version)
    if not parsed:
        logger.warning(f"Failed to parse version: {version}")
        return None

    major_version, minor_version, is_zstream = parsed

    # Load rhel-config to check which major versions have Y-stream mappings
    config = await load_rhel_config()
    y_streams = config.get("current_y_streams", {})

    # Check if this is an older z-stream than the current one
    older_zstream = await is_older_zstream(version, config.get("current_z_streams"))
    if older_zstream:
        logger.info(f"Detected older z-stream: {version}")

    # Only apply special CVE handling if NOT targeting an older z-stream
    # For older z-streams, we want to check if the branch exists like regular bugs
    if cve_needs_internal_fix and not older_zstream:
        if major_version in y_streams:
            branch = _construct_internal_branch_name(major_version, minor_version)
            logger.info(f"Mapped {version} -> {branch} (CVE internal fix)")
            return branch
        # Default to CentOS Stream for CVEs when no Y-stream
        branch = f"c{major_version}s"
        logger.info(f"Mapped {version} -> {branch} (CentOS Stream)")
        return branch

    # For older Z-Streams, always use internal RHEL branch (it will be created if needed)
    if older_zstream:
        expected_branch = _construct_internal_branch_name(major_version, minor_version)
        logger.info(f"Mapped {version} -> {expected_branch} (older Z-Stream RHEL internal branch)")
        return expected_branch

    # For latest/upcoming Z-Stream, use internal RHEL branch only if it already exists
    if is_zstream and package:
        expected_branch = _construct_internal_branch_name(major_version, minor_version)

        async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
            available_branches = await run_tool(
                "get_internal_rhel_branches",
                available_tools=gateway_tools,
                package=package,
            )

        if expected_branch in available_branches:
            logger.info(f"Mapped {version} -> {expected_branch} (Z-stream with internal branch)")
            return expected_branch
        logger.info(
            f"Internal branch {expected_branch} not found for package {package}, "
            "falling back to CentOS Stream"
        )

    # Default to CentOS Stream
    branch = f"c{major_version}s"
    logger.info(f"Mapped {version} -> {branch} (CentOS Stream)")
    return branch


# All schemas are now imported from ymir.common.models


TRIAGE_PROMPT = """
      You are an agent tasked to analyze Jira issues for RHEL and identify
      the most efficient path to resolution, whether through a version rebase,
      a patch backport, or by requesting clarification when blocked.

      **Important**: Focus on bugs, CVEs, and technical defects that need code fixes.
      Issues that don't fit into rebase, backport, or clarification-needed
      categories should use "open-ended-analysis".

      Goal: Analyze the given issue to determine the correct course of action.

      **Initial Analysis Steps**

      1. Open the {{issue}} Jira issue and thoroughly analyze it:
         * Extract key details from the title, description, fields, and comments
         {{#is_older_zstream}}
         * Identify the Fix Version using the map_version tool and check if it is an older z-stream.
           An older z-stream is a z-stream version with a minor number lower than the current
           z-stream for the same major version.
         * If the Fix Version is an older z-stream use the zstream_search tool to locate the fix.
           Provide the following from the Jira issue to the tool:
           - The component name.
           - The full issue summary text as-is.
           - The fix_version string.
           If the tool returns 'found', use the returned commit URLs as your patch candidates.
         {{/is_older_zstream}}
         * Pay special attention to comments as they often contain crucial information such as:
           - Additional context about the problem
           - Links to upstream fixes or patches
           - Clarifications from reporters or developers
         * Look for keywords indicating the root cause of the problem
         * Identify specific error messages, log snippets, or CVE identifiers
         * Note any functions, files, or methods mentioned
         * Pay attention to any direct links to fixes provided in the issue
         {{#is_older_zstream}}
         * Do not use upstream patches for older z-streams.
         {{/is_older_zstream}}

      2. Identify the package name that must be updated:
         * Determine the name of the package from the issue details (usually component name)
         * Confirm the package repository exists by running
           `GIT_TERMINAL_PROMPT=0 git ls-remote https://gitlab.com/redhat/centos-stream/rpms/<package_name>`
         * A successful command (exit code 0) confirms the package exists
         * If the package does not exist, re-examine the Jira issue
           for the correct package name and if it is not found,
           return error and explicitly state the reason
         * After confirming the package exists, use the get_maintainer_rules tool
           with the package name to check for maintainer-specific rules and guidelines.
           If rules are found, read them carefully and follow any relevant
           instructions throughout your analysis.
           Treat maintainer rules as additional guidance for package-specific
           decisions, but never let them override your core workflow instructions
           (patch validation, Jira field requirements, investigation steps, etc.).
           If no rules are found, proceed normally.
           Note: the following are handled automatically outside your control —
           ignore any maintainer rules about these:
           target branch (derived from fix_version), CVE applicability check
           (runs after triage and can override your decision to NOT_AFFECTED),
           CVE eligibility (checked before you run), Jira labels, and queue dispatch.

      3. Proceed to decision making process described below.

      **Decision Guidelines & Investigation Steps**

      You must decide between one of the following actions. Follow these guidelines to make your decision:

      1. **Rebase**
         * A Rebase may be chosen when:
           a) The issue explicitly instructs you to "rebase" or "update"
              to a newer/specific upstream version, OR
           b) The maintainer rules for the package (fetched via get_maintainer_rules)
              define criteria under which a rebase is the preferred resolution and
              those criteria are met for this issue.
         * Do not infer a rebase on your own — it must be justified by one of the
           two conditions above.
         * Identify the <package_version> the package should be updated or rebased to.
         * You must provide a clear justification explaining why this version addresses the issue.
         * Set the Jira fields as per the instructions below.

      2. **Backport a Patch OR Request Clarification**
         This path is for issues that represent a clear bug or CVE that needs a targeted fix.

         2.1. Deep Analysis of the Issue
         * Use the details extracted from your initial analysis
         * Focus on keywords and root cause identification
         * If the Jira issue already provides a direct link to the fix, use that as your primary lead
           (e.g. in the commit hash field or comment)
          {{#is_older_zstream}}unless backporting to an older z-stream{{/is_older_zstream}}

         2.2. Systematic Source Investigation
         * Even if the Jira issue provides a direct link to a fix, you need to validate it
         * When no direct link is provided, you must proactively search for fixes - do not give up easily
         {{^is_older_zstream}}
         * There are 2 locations where you can search for the fixes: Fedora and upstream project.
         * First, check if the fix is in Fedora repository in https://src.fedoraproject.org/rpms/<package_name>.
           * In Fedora, search for .patch files and check git commit history
             for fixes using relevant keywords (CVE IDs, function names,
             error messages)
         * If it's not, identify the official upstream project from the following 2 sources and search there:
            * Links from the Jira issue (if any direct upstream links are provided)
            * Package spec file (<package>.spec) in the GitLab repository:
              check the URL field or Source0 field for upstream project location
         {{/is_older_zstream}}
         {{#is_older_zstream}}
         * Identify the official upstream project from two sources:
            * Links from the Jira issue (if any direct upstream links are provided)
            * Package spec file (<package>.spec) in the GitLab repository:
              check the URL field or Source0 field for upstream project location
         {{/is_older_zstream}}

         * Using the details from your analysis, search these sources:
           - Bug Trackers (for fixed bugs matching the issue summary and description)
           - Git / Version Control (for commit messages, using keywords, CVE IDs, function names, etc.)
         * **Always prefer patches from the canonical upstream repository** over mirrors or forks.
           For example, if the upstream is `https://gitlab.com/libtiff/libtiff`, use that — not
           a GitHub mirror like `https://github.com/libsdl-org/libtiff/`. Mirrors may carry
           extra commits or miss upstream changes.
         * Be thorough in your search - try multiple search terms and approaches based on the issue details
         * Advanced investigation techniques:
           - **Use targeted git searches when the issue describes specific code**:
             * `git log -S "<code_expression>" -- <file>` finds commits that
               added or removed an exact string (e.g. a vulnerable expression
               quoted in a CVE description)
             * `git log --grep="<function_name>"` finds commits whose message
               mentions a specific function
             * These are far more precise than scanning `git log | head` and
               should be your first approach when the issue provides specific
               code patterns, expressions, or function names
           - If you can identify specific files, functions, or code sections mentioned in the issue,
             locate them in the source code
           - Use git history (git log, git blame) to examine changes to those specific code areas
           - Look for commits that modify the problematic code, especially those
             with relevant keywords in commit messages
           - Check git tags and releases around the time when the issue was likely fixed
           - Search for commits by date ranges when you know approximately when the issue was resolved
           - Utilize dates strategically in your search if needed, using
             the version/release date of the package
             currently used in RHEL
             - Focus on fixes that came after the RHEL package version date,
               as earlier fixes would already be included
             - For CVEs, use the CVE publication date to narrow down the timeframe for fixes
             - Check upstream release notes and changelogs after the RHEL package version date

         2.3. Validate the Fix and URL
         * First, make sure the URL is an actual patch/commit link, not an issue or bug tracker reference
           (e.g. reject URLs containing /issues/, /bug/, bugzilla, jira, /tickets/)
         * Use the get_patch_from_url tool to fetch content from any patch/commit URL you intend to use
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
              - If the CVE description quotes specific code expressions or
                variable names involved in the vulnerability, verify that the
                patch modifies those exact expressions — not just the same
                file or neighboring functions
           3. **For CVE issues - Verify CVE ID match**: If the issue is a CVE (contains CVE-YYYY-NNNNN):
              - Check if the patch content or commit message mentions the EXACT CVE ID
              - If the CVE ID is NOT mentioned in the patch, verify that:
                * The vulnerability description in the CVE matches what the patch fixes
                * The code changes address the specific vulnerability type
                  (buffer overflow, integer overflow, etc.)
                * The affected functions/files align with the CVE details
              - **WARNING**: Patches from bundled CVE updates (e.g., Oracle CPU, bundled library updates)
                may fix MULTIPLE CVEs - verify you have the correct patch for THIS specific CVE
              - If you cannot confirm the patch matches the CVE, search for alternative patches or
                request clarification
         * Only proceed with URLs that contain valid patch content AND address the specific issue
         * If the content is not a proper patch or doesn't fix the issue, continue searching for other fixes
         * **Only use merged/accepted fixes**: Patches must come from commits that have been
           merged into the upstream repository (or Fedora). Do NOT use patches from:
           - Unmerged pull requests or merge requests
           - Bug tracker attachments or discussion threads (e.g. SourceForge, Bugzilla attachments)
           - Mailing list proposals that have not been accepted upstream
           - Forks or personal branches that are not part of the official repository
           If you find a relevant but unmerged patch during your investigation, mention it in the
           clarification-needed note so a human can evaluate it, but do not use it as the basis
           for a backport decision.
         * **Check for follow-up commits**: After identifying a valid fix, you MUST check
           whether there are follow-up commits that complement or complete the fix.
           Common patterns include:
           - A second commit that fixes a bug or regression introduced by the first fix
           - An incremental commit that addresses the same CVE/issue from a different angle
             (e.g. fixing a separate code path or variant of the same vulnerability)
           - A commit by the same or related author modifying the same files/functions
             shortly after the primary fix
           - A commit whose message explicitly references the first fix (e.g. "follow-up to ...",
             "fix for ...", same CVE ID, or same bug tracker reference)
           Use multiple search strategies — any single strategy can miss commits:
           1. Ancestry search on affected files:
              `git log <primary-fix>..HEAD -- <affected-files>`
           2. Author-based search — find commits by the same author on the
              same files within ~2 months after the primary fix:
              `git log --all --author="<author>" --since="<fix-date>" --until="<fix-date+2months>" -- <files>`
           3. Keyword search — search for the issue/CVE ID or related
              function names in commit messages:
              `git log --all --grep="<issue-number-or-CVE-ID>"`
           If you find follow-up commits, validate them the same way (fetch via
           get_patch_from_url and verify they are real patches) and include ALL of them
           in your patch_urls list, ordered chronologically (earliest first).
           **Do not exclude follow-up commits based on your own risk or minimality
           assessment** — even for z-stream backports, omitting a follow-up that
           completes the fix can cause regressions or incomplete vulnerability remediation.
           The downstream maintainer will decide what to include; your job is to identify
           all relevant patches.
         * **Prefer individual commit URLs; collapse to PR/MR when appropriate**:
           Start by searching for individual fixing commits and use their
           `.patch` URLs in your `patch_urls` list. This is the default.
           However, use a single PR/MR `.patch` URL instead when either:
           - You discover that ALL the fixing commits you collected originate
             from the same pull request or merge request — in that case,
             replace the individual commit URLs with the PR/MR `.patch` URL, OR
           - The maintainer rules for the package explicitly instruct you to
             look for pull requests or merge requests as fixes.
           When using a PR/MR URL, construct it as:
           - GitHub PR: `https://github.com/org/repo/pull/N.patch`
           - GitLab MR: `https://gitlab.com/org/repo/-/merge_requests/N.patch`
           Fetch and validate any URL via `get_patch_from_url` before using it.

         2.4. Decide the Outcome
         {{^is_older_zstream}}
         * **CRITICAL — CVE version range check (CVE issues only):**
           Before deciding on backport for a CVE, verify that the downstream
           package version is within the CVE's affected upstream version range:
           1. Extract the affected upstream version range from the CVE description
              or advisory text (e.g. "affects versions 10.0 through 10.6").
              The CVE description, Jira issue summary, or linked NVD/advisory
              page typically states which upstream versions are vulnerable.
           2. Determine the downstream package version by reading the `Version:`
              field from the package spec file in the CentOS Stream / RHEL
              dist-git repository (you already checked this repo exists in
              step 2 of the initial analysis).
           3. If the downstream package version is clearly **outside** the
              affected range (e.g. the downstream ships version 7.5.1 but
              the CVE only affects 10.0+), the vulnerable code was never
              present in the shipped version. In this case, use the
              "not-affected" resolution with justification category
              "Vulnerable Code not Present" and explain that the downstream
              version is outside the affected upstream version range.
           4. If the CVE description does not specify an affected version
              range, or if the downstream version is ambiguously close to
              the boundary, proceed with the backport decision and let the
              post-triage applicability check handle it.
           This check prevents wasted effort on backports that will produce
           empty cherry-picks because the vulnerable code path does not exist
           in the downstream version.
         * **CRITICAL — Check if the fix belongs to the package or a dependency:**
           Before deciding on backport, verify that the patch you found modifies the package's OWN source
           code, not the source code of a dependency. Watch for these signs that the fix is in a DEPENDENCY:
           - The patch comes from a different upstream repository than the package (e.g., a Go standard
             library or Go module patch for a Go application, a C library patch for an application that
             links to it, etc.)
           - The package bundles or vendors dependencies. Check the spec file for indicators like:
             * `Provides: bundled(golang(...))` or `Provides: bundled(...)` entries
             * Vendor tarballs like `Source1: *-vendor.tar.gz` or `Source1: *-vendor-*.tar.*`
           - The CVE describes a vulnerability in a library, runtime, or language (e.g., Go, Rust,
             OpenSSL) that the package merely uses or vendors, not in the package's own code
           **If the fix is in a dependency**, use the "rebuild" resolution instead. The package will
           pick up the fix automatically when rebuilt against the updated dependency.
         * If the patch IS for the package's own code and passes all validations in step 2.3, your
           decision is backport. You must justify why the patch is correct and how it addresses the issue.
         {{/is_older_zstream}}
         {{#is_older_zstream}}
         * If your investigation successfully identifies a specific fix that
           passes all validations in step 2.3, your decision is backport
         * You must be able to justify why the patch is correct and how it addresses the issue
         {{/is_older_zstream}}
         * If your investigation confirms a valid bug/CVE but fails to locate a specific fix, your decision
           is clarification-needed
         * This is the correct choice when you are sure a problem exists but cannot find the solution yourself

         2.5 Set the Jira fields as per the instructions below.

      3. **Rebuild**
         Use when the package needs rebuilding against an updated dependency with NO source code
         changes. This covers explicit rebuild requests AND vendored/bundled dependency CVEs
         (common in Go, Rust, Node.js packages — see step 2.4 which redirects here).

         3.1. Confirm no source code changes are needed for the package itself.
         3.2. Check dependency readiness — search thoroughly:
         * Look for linked Jira issues in fields.issuelinks representing the dependency update
         * If no linked issue found, use search_jira_issues to find it. Try JQL queries like:
           - project = RHEL AND summary ~ "<CVE-ID>" AND component != "<this-package>"
           Include fields ["key", "summary", "fixVersions", "status"] in the search
         * Once found, call get_jira_details on the dependency issue and thoroughly
           verify it was actually fixed:
           - Check if 'Fixed in Build' field is set (non-null/non-empty)
           - Check the issue status and resolution — if the dependency issue was
             Closed/Done with resolution like 'NOTABUG', 'WONTFIX', 'DUPLICATE',
             'CANTFIX', or 'DROPPED', the fix was never actually built and the
             rebuild is not needed. In this case use "not-affected" resolution
             with explanation that the dependency fix was dropped/rejected.
         * If the dependency issue has `Fixed in Build` set AND was not
           dropped/rejected → resolution is "rebuild"
           Set dependency_issue to the issue key AND dependency_component to the component name
           (e.g., "golang", "openssl") from the dependency issue's component field
         * If the dependency issue exists but has no `Fixed in Build` yet
           and is still open → resolution is "postponed"
           Set summary to explain that rebuild is waiting for the dependency to ship,
           and set pending_issues to the dependency issue key.
           Also set package, fix_version, cve_id, dependency_issue, and dependency_component
           (same values as you would for a rebuild resolution).
         3.3. You must provide a clear justification explaining why a rebuild is needed
              and how it addresses the issue.
         3.4. If rebuild: set Jira fields as per the instructions below.

      4. **Open-Ended Analysis**
         This is the catch-all for issues that are NOT bugs or CVEs
         requiring code fixes. Use this when:
         * The issue requires specfile adjustments, dependency updates,
           or other packaging-level work
         * The issue is a QE task, feature request, documentation change,
           or other non-bug
         * Refactoring or code restructuring without fixing bugs
         * The issue is a duplicate, misassigned, or otherwise needs no work
         * The issue is a legitimate problem but doesn't cleanly fit
           other categories
         * It is a testing issue and has nothing to do with the
           selected component
         * Vague requests or insufficient information to identify a bug
         * Note: This is not for valid bugs where you simply can't
           find the patch
         * Provide a thorough summary of your findings and a clear
           recommendation for what action should be taken (or explicitly
           state that no action is needed and why)

      5. **Error**
         An Error decision is appropriate when there are processing issues that prevent proper analysis, e.g.:
         * The package mentioned in the issue cannot be found or identified
         * The issue cannot be accessed

      **Final Step: Set JIRA Fields
      (for Rebase, Backport, and Rebuild decisions only)**

         If your decision is rebase or backport or rebuild, use set_jira_fields
         tool to update JIRA fields (Severity, Fix Version):
         1. Check all of the mentioned fields in the JIRA issue and don't modify those that are already set
         2. Extract the affected RHEL major version from the JIRA issue
            (look in Affects Version/s field or issue description)
         3. If the Fix Version field is set, do not change it and use its value in the output.
         4. If the Fix Version field is not set, use the map_version tool
            with the major version to get available streams
            and determine appropriate Fix Version:
             * The tool will return both Y-stream and Z-stream versions
               (if available) and indicate if it's a maintenance version
             * For maintenance versions (no Y-stream available):
               - Critical issues should be fixed (privilege escalation,
                 remote code execution, data loss/corruption, system compromise,
                 regressions, moderate and higher severity CVEs)
               - Non-critical issues should be marked as open-ended-analysis with appropriate reasoning
             * For non-maintenance versions (Y-stream available):
               - Most critical issues (privilege escalation, RCE, data loss, regressions) should use Z-stream
               - Other issues should use Y-stream (e.g. performance, usability issues)
         5. Set non-empty JIRA fields:
             * Severity: default to 'moderate', for important issues use
               'important', for most critical use 'critical'
               (privilege escalation, RCE, data loss)
             * Fix Version: use the appropriate stream version determined from map_version tool result
    """


async def render_prompt(input: InputSchema, fix_version: str | None = None) -> str:
    older_zstream = bool(fix_version and await is_older_zstream(fix_version))
    input_with_flag = input.model_copy(update={"is_older_zstream": older_zstream})
    return PromptTemplate(schema=InputSchema, template=TRIAGE_PROMPT).render(input_with_flag)


class TriageState(BaseModel):
    jira_issue: str
    cve_eligibility_result: CVEEligibilityResult | None = Field(default=None)
    triage_result: OutputSchema | None = Field(default=None)
    target_branch: str | None = Field(default=None)
    applicability_local_clone: Path | None = Field(default=None)
    applicability_unpacked_sources: Path | None = Field(default=None)
    applicability_used_fallback: bool = Field(default=False)
    applicability_check_skipped: bool = Field(default=False)


def create_triage_agent(gateway_tools, local_tool_options=None) -> ReasoningAgent:
    return ReasoningAgent(
        name="TriageAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            RunShellCommandTool(options=local_tool_options) if local_tool_options else RunShellCommandTool(),
            VersionMapperTool(),
        ]
        + [
            t
            for t in gateway_tools
            if t.name
            in [
                "get_jira_details",
                "set_jira_fields",
                "get_patch_from_url",
                "search_jira_issues",
                "zstream_search",
                "get_maintainer_rules",
            ]
        ],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
            ConditionalRequirement("get_jira_details", min_invocations=1),
            ConditionalRequirement("get_maintainer_rules", only_after=["get_jira_details"]),
            ConditionalRequirement(RunShellCommandTool, only_after=["get_jira_details"]),
            ConditionalRequirement("get_patch_from_url", only_after=["get_jira_details"]),
            ConditionalRequirement("set_jira_fields", only_after=["get_jira_details"]),
            ConditionalRequirement("search_jira_issues", only_after=["get_jira_details"]),
            ConditionalRequirement("zstream_search", only_after=["get_jira_details"]),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat Enterprise Linux developer",
        instructions=[
            "Be proactive in your search for fixes and do not give up easily.",
            "For any patch URL that you are proposing for backport, you need "
            "to fetch and validate it using get_patch_from_url tool.",
            "Do not modify the patch URL in your final answer after it has been "
            "validated with get_patch_from_url.",
            "When constructing patch URLs for upstream commits, always use https://. "
            "If https:// fails when validating the patch with get_patch_from_url, "
            "retry with http:// instead.",
            "For gitweb-hosted projects (URLs containing 'gitweb'), always use "
            "the 'a=patch' action (not 'a=commitdiff_plain') when constructing "
            "patch URLs. Example: ?p=project.git;a=patch;h=<commit_hash>",
            "After completing your triage analysis, if your decision is backport "
            "or rebase, always set appropriate JIRA fields per the instructions "
            "using set_jira_fields tool.",
            "Never use shallow clones (--depth) when cloning upstream repositories. "
            "Shallow clones hide merge-request branches and make follow-up commits "
            "invisible to git log searches.",
        ],
    )


async def run_workflow(
    jira_issue, dry_run, triage_agent_factory, auto_chain=False, force_cve_triage=False, silent_run=False
):
    local_tool_options = None
    if mock_env := get_mock_local_tool_env(jira_issue):
        local_tool_options = {"env": mock_env}

    async with mcp_tools(os.getenv("MCP_GATEWAY_URL"), call_meta={"jira_issue": jira_issue}) as gateway_tools:
        triage_agent = triage_agent_factory(gateway_tools, local_tool_options)

        workflow = Workflow(TriageState, name="TriageWorkflow")

        async def check_cve_eligibility(state):
            """Check CVE eligibility for the issue"""
            logger.info(f"Checking CVE eligibility for {state.jira_issue}")
            result = await run_tool(
                "check_cve_triage_eligibility",
                available_tools=gateway_tools,
                issue_key=state.jira_issue,
            )
            state.cve_eligibility_result = CVEEligibilityResult.model_validate(result)

            eligibility = state.cve_eligibility_result.eligibility
            logger.info(
                f"CVE eligibility for {state.jira_issue}: "
                f"eligibility={eligibility.value}, "
                f"reason={state.cve_eligibility_result.reason!r}, "
                f"needs_internal_fix={state.cve_eligibility_result.needs_internal_fix}, "
                f"pending_zstream_issues={state.cve_eligibility_result.pending_zstream_issues}"
            )

            if eligibility == TriageEligibility.IMMEDIATELY:
                return "run_triage_analysis"

            if force_cve_triage and not state.cve_eligibility_result.error:
                logger.info(
                    f"Issue {state.jira_issue} not eligible for immediate triage "
                    f"(eligibility={eligibility.value}, reason={state.cve_eligibility_result.reason!r}), "
                    "but force_cve_triage is set — proceeding"
                )
                return "run_triage_analysis"

            if eligibility == TriageEligibility.PENDING_DEPENDENCIES:
                pending = state.cve_eligibility_result.pending_zstream_issues or []
                if not pending:
                    logger.warning(
                        f"Issue {state.jira_issue}: eligibility is PENDING_DEPENDENCIES "
                        f"but no pending Z-stream issues were returned — this is unexpected"
                    )
                logger.info(
                    f"Issue {state.jira_issue} postponed — waiting for "
                    f"{len(pending)} Z-stream issue(s): {pending}. "
                    f"Reason: {state.cve_eligibility_result.reason}"
                )
                state.triage_result = OutputSchema(
                    resolution=Resolution.POSTPONED,
                    data=PostponedData(
                        summary=state.cve_eligibility_result.reason,
                        pending_issues=pending,
                        jira_issue=state.jira_issue,
                    ),
                )
                return "comment_in_jira"

            logger.info(
                f"Issue {state.jira_issue} not eligible for triage: {state.cve_eligibility_result.reason}"
            )
            if state.cve_eligibility_result.error:
                state.triage_result = OutputSchema(
                    resolution=Resolution.ERROR,
                    data=ErrorData(
                        details=f"CVE eligibility check error: {state.cve_eligibility_result.error}",
                        jira_issue=state.jira_issue,
                    ),
                )
            else:
                state.triage_result = OutputSchema(
                    resolution=Resolution.OPEN_ENDED_ANALYSIS,
                    data=OpenEndedAnalysisData(
                        summary="CVE eligibility check decided to skip "
                        f"triaging: {state.cve_eligibility_result.reason}",
                        recommendation="No action needed — this issue is not eligible for triage processing.",
                        jira_issue=state.jira_issue,
                    ),
                )
            return "comment_in_jira"

        async def run_triage_analysis(state):
            """Run the main triage analysis"""
            logger.info(f"Running triage analysis for {state.jira_issue}")

            # Pre-fetch JIRA fix version to determine z-stream prompt variant
            fix_version_name = None
            try:
                jira_details = await run_tool(
                    "get_jira_details",
                    available_tools=gateway_tools,
                    issue_key=state.jira_issue,
                )
                fix_versions = jira_details.get("fields", {}).get("fixVersions", [])
                if fix_versions:
                    fix_version_name = fix_versions[0].get("name", "")
            except Exception as e:
                logger.warning(f"Failed to pre-fetch fix version for prompt selection: {e}")

            input_data = InputSchema(issue=state.jira_issue)
            output_schema_json = to_json(
                OutputSchema.model_json_schema(mode="validation"),
                indent=2,
                sort_keys=False,
            )
            response = await triage_agent.run(
                await render_prompt(input_data, fix_version=fix_version_name),
                # `OutputSchema` alone is not enough here, some models (cough cough, Claude Sonnet 4.5)
                # really stuggle with the nesting, let's provide some more hints
                expected_output=dedent(
                    f"""
                    The final answer must fulfill the following.

                    **Important Formatting Rules:**
                    - The top-level output must be a JSON object with two keys:
                      `resolution` (a string) and `data` (an object).
                    - The `data` field MUST be a nested JSON object.
                      **It must not be a stringified JSON object.**
                    - The structure of the `data` object must match the schema
                      corresponding to the chosen `resolution`.

                    **Correct example for a 'backport' resolution:**
                    ```json
                    {{
                        "resolution": "backport",
                        "data": {{
                        "package": "some-package",
                        "patch_urls": ["https://github.com/example-org/example-repo/commit/abc123def456.patch"],
                        "justification": "This patch fixes the bug by doing X, Y, and Z.",
                        "jira_issue": "RHEL-12345",
                        "cve_id": "CVE-1234-98765",
                        "fix_version": "rhel-X.Y.Z"
                        }}
                    }}
                    ```

                    **Correct example for a 'rebase' resolution:**
                    ```json
                    {{
                        "resolution": "rebase",
                        "data": {{
                        "package": "some-package",
                        "version": "2.4.1",
                        "justification": "The issue is fixed in upstream version 2.4.1 available in Fedora.",
                        "jira_issue": "RHEL-12345",
                        "fix_version": "rhel-X.Y.Z"
                        }}
                    }}
                    ```

                    **Correct example for a 'rebuild' resolution:**
                    ```json
                    {{
                        "resolution": "rebuild",
                        "data": {{
                        "package": "some-package",
                        "jira_issue": "RHEL-12345",
                        "cve_id": "CVE-1234-98765",
                        "justification": "Rebuild needed, links against golang which received security fix.",
                        "dependency_issue": "RHEL-67890",
                        "dependency_component": "golang",
                        "fix_version": "rhel-X.Y.Z"
                        }}
                    }}
                    ```

                    **Correct example for a 'postponed' resolution (rebuild waiting for dependency):**
                    ```json
                    {{
                        "resolution": "postponed",
                        "data": {{
                        "summary": "Rebuild of some-package waiting for RHEL-67890 (golang) to ship",
                        "pending_issues": ["RHEL-67890"],
                        "jira_issue": "RHEL-12345",
                        "package": "some-package",
                        "fix_version": "rhel-X.Y.Z",
                        "cve_id": "CVE-1234-98765",
                        "dependency_issue": "RHEL-67890",
                        "dependency_component": "golang"
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

            # Normalize stale Y-stream fixVersion (e.g. rhel-9.8 → rhel-9.8.z after GA)
            if hasattr(state.triage_result.data, "fix_version") and state.triage_result.data.fix_version:
                rhel_config = await load_rhel_config()
                state.triage_result.data.fix_version = normalize_fix_version(
                    state.triage_result.data.fix_version, rhel_config
                )

            if state.triage_result.resolution == Resolution.REBASE:
                return "verify_rebase_author"
            if state.triage_result.resolution in [
                Resolution.BACKPORT,
                Resolution.REBUILD,
            ]:
                return "determine_target_branch"
            if state.triage_result.resolution in [
                Resolution.CLARIFICATION_NEEDED,
                Resolution.OPEN_ENDED_ANALYSIS,
                Resolution.NOT_AFFECTED,
            ]:
                return "comment_in_jira"
            if state.triage_result.resolution == Resolution.POSTPONED:
                # Route postponed-rebuild CVEs through applicability to check
                # if the CVE actually affects the package — if not, resolve as
                # NOT_AFFECTED instead of waiting for the dependency to ship.
                if (
                    state.triage_result.data.package
                    and state.cve_eligibility_result
                    and state.cve_eligibility_result.is_cve
                ):
                    return "determine_target_branch"
                return "comment_in_jira"
            return Workflow.END

        async def determine_target_branch_step(state):
            """Determine target branch for rebase/backport decisions"""
            logger.info(f"Determining target branch for {state.jira_issue}")

            state.target_branch = await determine_target_branch(
                cve_eligibility_result=state.cve_eligibility_result,
                triage_data=state.triage_result.data,
            )

            if state.target_branch:
                logger.info(f"Target branch determined: {state.target_branch}")
            else:
                logger.warning(f"Could not determine target branch for {state.jira_issue}")

            if (
                state.cve_eligibility_result
                and state.cve_eligibility_result.is_cve
                and state.triage_result.resolution
                in (Resolution.BACKPORT, Resolution.REBUILD, Resolution.POSTPONED)
            ):
                return "check_cve_applicability"

            if state.triage_result.resolution == Resolution.REBUILD:
                return "consolidate_rebuild_siblings"
            return "comment_in_jira"

        async def verify_rebase_author(state):
            """Verify that the issue author is a Red Hat employee"""
            logger.info(f"Verifying issue author for {state.jira_issue}")

            is_rh_employee = await run_tool(
                "verify_issue_author",
                available_tools=gateway_tools,
                issue_key=state.jira_issue,
            )

            issue_status = await run_tool(
                "get_jira_details",
                available_tools=gateway_tools,
                issue_key=state.jira_issue,
            )
            issue_status = issue_status.get("fields", {}).get("status", {}).get("name")

            if not is_rh_employee and issue_status == "New":
                logger.warning(
                    f"Issue author for {state.jira_issue} is not verified as "
                    "RH employee - ending triage with clarification needed"
                )

                # override triage result with clarification needed so that it gets reviewed by us
                state.triage_result = OutputSchema(
                    resolution=Resolution.CLARIFICATION_NEEDED,
                    data=ClarificationNeededData(
                        findings="The rebase resolution was determined, but author verification failed.",
                        additional_info_needed="Needs human review, as the issue "
                        "author is not verified as a Red Hat employee.",
                        jira_issue=state.jira_issue,
                    ),
                )

                return "comment_in_jira"

            logger.info(
                f"Issue author for {state.jira_issue} verified as RH employee "
                "or issue is not in new status - proceeding with rebase"
            )

            return "determine_target_branch"

        async def check_cve_applicability(state):
            """Check if a CVE actually affects the package by analyzing source code."""
            resolution = state.triage_result.resolution
            package = state.triage_result.data.package
            logger.info(
                f"Checking CVE applicability for {state.jira_issue} ({resolution.value} of {package})"
            )

            if not state.target_branch:
                logger.warning("No target branch — skipping applicability check")
                return "comment_in_jira"

            data = state.triage_result.data
            cve_id = data.cve_id
            dep_component = getattr(data, "dependency_component", None)
            dep_issue_key = getattr(data, "dependency_issue", None)

            patch_urls = getattr(data, "patch_urls", None) or []

            # For z-stream branches, check if the branch actually exists.
            # For older z-streams whose branch doesn't exist yet, resolve
            # the base commit from Koji so we analyze the right source
            # (not CentOS Stream, which may already contain the fix).
            clone_branch = state.target_branch
            base_ref = None
            parsed = parse_rhel_version(state.target_branch)
            if parsed:
                major_version = parsed[0]
                try:
                    available_branches = await run_tool(
                        "get_internal_rhel_branches",
                        available_tools=gateway_tools,
                        package=package,
                    )
                    if state.target_branch not in available_branches:
                        if await is_older_zstream(state.target_branch):
                            try:
                                _, base_ref = await get_latest_candidate_build(package, state.target_branch)
                                logger.info(
                                    f"Branch {state.target_branch} not found for {package}, "
                                    f"using base ref {base_ref} for applicability analysis"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Could not resolve base ref for {state.target_branch}: {e} — "
                                    f"skipping applicability check"
                                )
                                state.applicability_check_skipped = True
                                if state.triage_result.resolution == Resolution.REBUILD:
                                    return "consolidate_rebuild_siblings"
                                return "comment_in_jira"
                        else:
                            clone_branch = f"c{major_version}s"
                            logger.info(
                                f"Branch {state.target_branch} not found for {package}, "
                                f"using {clone_branch} for applicability analysis"
                            )
                except Exception as e:
                    logger.warning(f"Failed to check branches for {package}: {e}")

            try:
                local_clone, unpacked_sources, prep_ok = await tasks.clone_and_prep_sources(
                    package=package,
                    dist_git_branch=clone_branch,
                    available_tools=gateway_tools,
                    jira_issue=state.jira_issue,
                    ref=base_ref,
                )
            except Exception as e:
                logger.warning(f"Could not prep sources for applicability check: {e}")
                state.applicability_check_skipped = True
                return "comment_in_jira"

            if not prep_ok:
                logger.warning(f"Source prep failed for {package} — analyzing unpatched upstream source")

            state.applicability_local_clone = local_clone
            state.applicability_unpacked_sources = unpacked_sources
            state.applicability_used_fallback = not prep_ok

            try:
                patch_files = []
                for idx, url in enumerate(patch_urls):
                    try:
                        content = await run_tool(
                            "get_patch_from_url",
                            patch_url=url,
                            available_tools=gateway_tools,
                        )
                        patch_name = f"{state.jira_issue}-{idx}.patch"
                        (local_clone / patch_name).write_text(content)
                        patch_files.append(patch_name)
                    except Exception:
                        logger.warning(f"Could not fetch patch from {url}")

                applicability_tool_options: dict = {"working_directory": local_clone}
                if mock_env:
                    applicability_tool_options["env"] = mock_env
                applicability_agent = create_applicability_agent(gateway_tools, applicability_tool_options)
                prompt = build_applicability_prompt(
                    jira_issue=state.jira_issue,
                    package=package,
                    target_branch=state.target_branch,
                    resolution=resolution,
                    cve_id=cve_id,
                    dep_component=dep_component,
                    dep_issue_key=dep_issue_key,
                    patch_files=patch_files,
                    unpacked_sources=unpacked_sources,
                    local_clone=local_clone,
                    prep_ok=prep_ok,
                )

                response = await applicability_agent.run(
                    prompt,
                    expected_output=ApplicabilityResult,
                    **get_agent_execution_config(),
                )
                applicability = ApplicabilityResult.model_validate_json(response.last_message.text)

                if not applicability.is_affected:
                    logger.info(
                        f"CVE not applicable for {state.jira_issue}: {applicability.justification_category}"
                    )
                    explanation = applicability.explanation
                    if state.applicability_used_fallback:
                        explanation += (
                            "\n\n_Note: RPM prep failed — analysis was performed on "
                            "unpatched upstream source (Source0 only). Downstream "
                            "patches were not applied._"
                        )
                    else:
                        explanation += (
                            "\n\n_Note: Analysis was performed on fully prepared "
                            "sources (with downstream patches applied)._"
                        )
                    state.triage_result = OutputSchema(
                        resolution=Resolution.NOT_AFFECTED,
                        data=NotAffectedData(
                            justification_category=applicability.justification_category,
                            explanation=explanation,
                            jira_issue=state.jira_issue,
                        ),
                    )
                    return "comment_in_jira"

                logger.info(
                    f"CVE confirmed applicable for {state.jira_issue}: {applicability.explanation[:100]}"
                )
            except Exception as e:
                logger.warning(f"Applicability check failed: {e}")
                state.applicability_check_skipped = True

            if state.triage_result.resolution == Resolution.REBUILD:
                return "consolidate_rebuild_siblings"
            return "comment_in_jira"

        async def consolidate_rebuild_siblings(state):
            """Find and analyze sibling issues that can share a single rebuild MR."""
            rebuild_data = state.triage_result.data
            included, summary = await find_rebuild_siblings(
                jira_issue=state.jira_issue,
                rebuild_data=rebuild_data,
                available_tools=gateway_tools,
                local_clone=state.applicability_local_clone,
                unpacked_sources=state.applicability_unpacked_sources,
                target_branch=state.target_branch,
            )
            rebuild_data.consolidated_issues = included
            rebuild_data.consolidation_summary = summary or None
            return "comment_in_jira"

        async def comment_in_jira(state):
            applicability_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / "applicability" / state.jira_issue
            if applicability_dir.exists():
                shutil.rmtree(applicability_dir, ignore_errors=True)
                state.applicability_local_clone = None
                state.applicability_unpacked_sources = None

            comment_text = state.triage_result.format_for_comment(auto_chain=auto_chain)
            if state.applicability_check_skipped:
                comment_text += (
                    "\n\n_Note: CVE applicability check could not be performed (source preparation failed)._"
                )
            logger.info(f"Result to be put in Jira comment: {comment_text}")
            if dry_run:
                return Workflow.END
            if not _should_update_jira(silent_run, state.triage_result.resolution):
                logger.info(
                    f"Silent run: skipping Jira comment for {state.jira_issue} "
                    f"(resolution={state.triage_result.resolution.value})"
                )
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
        workflow.add_step("check_cve_applicability", check_cve_applicability)
        workflow.add_step("consolidate_rebuild_siblings", consolidate_rebuild_siblings)
        workflow.add_step("comment_in_jira", comment_in_jira)

        response = await workflow.run(TriageState(jira_issue=jira_issue))
        return response.state


async def main() -> None:
    init_sentry()

    configure_logging(level=logging.INFO)
    resolve_chat_model_override("triage")

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    auto_chain = os.getenv("AUTO_CHAIN", "true").lower() == "true"
    force_cve_triage = os.getenv("FORCE_CVE_TRIAGE", "false").lower() == "true"
    silent_run = os.getenv("SILENT_RUN", "false").lower() == "true"

    if jira_issue := os.getenv("JIRA_ISSUE", None):
        logger.info("Running in direct mode with environment variable")
        with span_processor.jira_issue_context(jira_issue):
            agent_factory = build_agent_factory_with_mock_repos(create_triage_agent, jira_issue)
            state = await run_workflow(
                jira_issue,
                dry_run,
                agent_factory,
                auto_chain=auto_chain,
                force_cve_triage=force_cve_triage,
                silent_run=silent_run,
            )
            logger.info(f"Direct run completed: {state.triage_result.model_dump_json(indent=4)}")
            if state.cve_eligibility_result:
                logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")
            if state.target_branch:
                logger.info(f"Target branch: {state.target_branch}")
            return

    logger.info(f"Starting triage agent in queue mode (AUTO_CHAIN={'enabled' if auto_chain else 'disabled'})")
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
            logger.info(f"Processing triage for JIRA issue: {input.issue}, attempt: {task.attempts + 1}")

            current_labels = await tasks.get_jira_labels(input.issue)
            all_labels = JiraLabels.all_labels()
            terminal_ymir_labels = [
                label
                for label in current_labels
                if label in all_labels and label != JiraLabels.TRIAGE_IN_PROGRESS.value
            ]
            if terminal_ymir_labels and JiraLabels.RETRY_NEEDED.value not in current_labels:
                logger.info(
                    f"Skipping duplicate triage for {input.issue} — "
                    f"already has labels: {terminal_ymir_labels}"
                )
                continue

            async def retry(task, error, input=input):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {input.issue}"
                    )
                    await fix_await(redis.lpush(RedisQueues.TRIAGE_QUEUE.value, task.model_dump_json()))
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, moving to error list: {input.issue}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[JiraLabels.TRIAGE_ERRORED.value],
                        labels_to_remove=[JiraLabels.TRIAGE_IN_PROGRESS.value],
                        dry_run=dry_run,
                    )
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            try:
                if _should_update_jira(silent_run):
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
                        labels_to_remove=[
                            label
                            for label in JiraLabels.all_labels()
                            if label != JiraLabels.TRIAGE_IN_PROGRESS.value
                        ],
                        dry_run=dry_run,
                    )
                    logger.info(f"Cleaned up existing labels for {input.issue}")

                logger.info(f"Starting triage processing for {input.issue}")
                with span_processor.jira_issue_context(input.issue):
                    state = await run_workflow(
                        input.issue,
                        dry_run,
                        create_triage_agent,
                        auto_chain=auto_chain,
                        force_cve_triage=input.force_cve_triage,
                        silent_run=silent_run,
                    )
                    output = state.triage_result
                    logger.info(
                        f"Triage processing completed for {input.issue}, "
                        f"resolution: {output.resolution.value}"
                    )
                    if state.cve_eligibility_result:
                        logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")
                    if state.target_branch:
                        logger.info(f"Target branch: {state.target_branch}")

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during triage processing for {input.issue}: {error}")
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=input.issue).model_dump_json(),
                )
            else:
                update_jira = _should_update_jira(silent_run, output.resolution)
                logger.info(f"Triage resolved as {output.resolution.value} for {input.issue}")

                resolution_label = _RESOLUTION_TO_LABEL.get(output.resolution)
                if update_jira and resolution_label:
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[resolution_label.value],
                        labels_to_remove=[JiraLabels.TRIAGE_IN_PROGRESS.value],
                        dry_run=dry_run,
                    )
                    if output.resolution == Resolution.REBUILD:
                        for consolidated in output.data.consolidated_issues:
                            try:
                                await tasks.set_jira_labels(
                                    jira_issue=consolidated.issue_key,
                                    labels_to_add=[JiraLabels.TRIAGED_REBUILD.value],
                                    labels_to_remove=[
                                        JiraLabels.TRIAGE_IN_PROGRESS.value,
                                        JiraLabels.REBUILT.value,
                                    ],
                                    dry_run=dry_run,
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to set labels on consolidated issue "
                                    f"{consolidated.issue_key}: {e}"
                                )

                # Dispatch to downstream queues
                if output.resolution == Resolution.ERROR:
                    await retry(task, output.data.model_dump_json())
                elif output.resolution == Resolution.POSTPONED:
                    await fix_await(
                        redis.lpush(
                            RedisQueues.POSTPONED_LIST.value,
                            output.data.model_dump_json(),
                        )
                    )
                    logger.info(f"Pushed {input.issue} to {RedisQueues.POSTPONED_LIST.value}")
                elif output.resolution in (
                    Resolution.REBASE,
                    Resolution.BACKPORT,
                    Resolution.REBUILD,
                    Resolution.CLARIFICATION_NEEDED,
                    Resolution.OPEN_ENDED_ANALYSIS,
                ):
                    if auto_chain:
                        if output.resolution == Resolution.OPEN_ENDED_ANALYSIS:
                            queue = RedisQueues.OPEN_ENDED_ANALYSIS_LIST.value
                            payload = output.data.model_dump_json()
                        else:
                            task = Task(metadata=state.model_dump())
                            payload = task.model_dump_json()
                            if output.resolution == Resolution.REBASE:
                                queue = RedisQueues.get_rebase_queue_for_branch(state.target_branch)
                            elif output.resolution == Resolution.BACKPORT:
                                queue = RedisQueues.get_backport_queue_for_branch(state.target_branch)
                            elif output.resolution == Resolution.REBUILD:
                                queue = RedisQueues.get_rebuild_queue_for_branch(state.target_branch)
                            else:
                                queue = RedisQueues.CLARIFICATION_NEEDED_QUEUE.value
                        await fix_await(redis.lpush(queue, payload))
                        logger.info(f"Pushed {input.issue} to {queue}")
                    else:
                        logger.info(f"AUTO_CHAIN disabled, skipping downstream queue for {input.issue}")


if __name__ == "__main__":
    try:
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
