import asyncio
import logging
import os
import re
import shutil
import sys
import traceback
from pathlib import Path

import sentry_sdk
from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools.think import ThinkTool
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
    render_template,
    resolve_chat_model_override,
    run_tool,
)
from ymir.common.base_utils import fix_await, install_shutdown_handler, redis_client, run_task_loop
from ymir.common.config import load_rhel_config
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.logging_setup import configure_logging, current_jira_issue, get_trajectory_writeable
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    ApplicabilityResult,
    ClarificationNeededData,
    CVEEligibilityResult,
    ErrorData,
    IssueStatus,
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
from ymir.common.utils import (
    FIXED_IN_BUILD_CUSTOM_FIELD,
    check_build_in_buildroot,
    get_latest_candidate_build,
)
from ymir.common.version_utils import (
    construct_internal_branch_name,
    is_older_zstream,
    normalize_fix_version,
    parse_rhel_version,
)
from ymir.tools.privileged.utils import APPLICABILITY_DIR
from ymir.tools.unprivileged.commands import RunShellCommandTool

## UpstreamSearchTool is currently unmaintained and disabled.
# from ymir.tools.unprivileged.upstream_search import UpstreamSearchTool
from ymir.tools.unprivileged.version_mapper import VersionMapperTool

logger = logging.getLogger(__file__)
redis_logger = logging.getLogger("agent.redis")


def _should_update_jira(resolution: Resolution = None, user_triggered: bool = False) -> bool:
    """Whether to post a user-facing Jira comment for this run.

    Used only for comments — labels are dedup anchors and are written
    unconditionally. Default is silent: comments are suppressed unless the
    run was explicitly requested by a maintainer (via ymir_todo) or the
    resolution carries information the requester needs even unbidden.
    The unbidden cases are the resolutions that do NOT produce an MR —
    without a comment the result would be invisible to the requester:
    not-affected, postponed, open-ended-analysis, clarification-needed.
    """
    if user_triggered:
        return True
    return resolution in (
        Resolution.NOT_AFFECTED,
        Resolution.POSTPONED,
        Resolution.OPEN_ENDED_ANALYSIS,
        Resolution.CLARIFICATION_NEEDED,
    )


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


_MODULAR_SUMMARY_RE = re.compile(r"^[\w.+-]+:[^/]+/[\w.+-]+:")


def _is_modular(jira_summary: str | None) -> bool:
    """Detect whether the Jira ticket targets a modular package.

    Modular summaries follow the pattern ``module:stream/component:Title``
    (e.g. ``postgresql:12/postgresql:PostgreSQL: some vulnerability``),
    while non-modular ones are ``component:Title``.
    """
    if not jira_summary:
        return False
    return bool(_MODULAR_SUMMARY_RE.match(jira_summary))


def _parse_module_summary(summary: str) -> tuple[str, str] | None:
    """Extract module name and stream from a modular Jira summary.

    E.g. ``postgresql:12/postgresql:...`` → ``("postgresql", "12")``.
    """
    m = _MODULAR_SUMMARY_RE.match(summary)
    if not m:
        return None
    prefix = summary[: m.end() - 1]  # "postgresql:12/postgresql"
    module_stream, _, _ = prefix.partition("/")
    module, _, stream = module_stream.partition(":")
    return module, stream


async def _map_version_to_module_branch(
    version: str, summary: str, cve_needs_internal_fix: bool, package: str | None = None
) -> str | None:
    """Map version string to a modular target branch.

    Branch format: ``stream-{module}-{stream}-rhel-{major}.{minor}.0``
    E.g. version ``rhel-9.8`` + summary ``postgresql:12/...``
    → ``stream-postgresql-12-rhel-9.8.0``
    """
    parsed_version = parse_rhel_version(version)
    if not parsed_version:
        logger.warning(f"Failed to parse version for modular branch: {version}")
        return None

    parsed_module = _parse_module_summary(summary)
    if not parsed_module:
        logger.warning(f"Failed to parse module/stream from summary: {summary!r}")
        return None

    major, minor, _ = parsed_version
    module, stream = parsed_module
    branch = f"stream-{module}-{stream}-rhel-{major}.{minor}.0"
    logger.info(f"Mapped {version} -> {branch} (modular)")
    return branch


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
    jira_summary = triage_data.summary if hasattr(triage_data, "summary") else None

    if _is_modular(jira_summary):
        branch = await _map_version_to_module_branch(
            triage_data.fix_version, jira_summary, cve_needs_internal_fix, package
        )
        jira_issue = getattr(triage_data, "jira_issue", "unknown")
        logger.info(
            f"Modular package detected for {jira_issue} "
            f"(summary={jira_summary!r}, would map to branch={branch}) — "
            f"skipping automated processing (not yet supported)"
        )
        # TODO: not yet implemented, this is the first step of modular support
        return None

    return await _map_version_to_branch(triage_data.fix_version, cve_needs_internal_fix, package)


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
            branch = construct_internal_branch_name(major_version, minor_version)
            logger.info(f"Mapped {version} -> {branch} (CVE internal fix)")
            return branch
        # Default to CentOS Stream for CVEs when no Y-stream
        branch = f"c{major_version}s"
        logger.info(f"Mapped {version} -> {branch} (CentOS Stream)")
        return branch

    # For older Z-Streams, always use internal RHEL branch (it will be created if needed)
    if older_zstream:
        expected_branch = construct_internal_branch_name(major_version, minor_version)
        logger.info(f"Mapped {version} -> {expected_branch} (older Z-Stream RHEL internal branch)")
        return expected_branch

    # For latest/upcoming Z-Stream, use internal RHEL branch only if it already exists
    if is_zstream and package:
        expected_branch = construct_internal_branch_name(major_version, minor_version)

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


async def render_prompt(
    input: InputSchema,
    fix_version: str | None = None,
    cve_eligibility_result: CVEEligibilityResult | None = None,
) -> str:
    older_zstream = bool(fix_version and await is_older_zstream(fix_version))

    updates: dict = {"is_older_zstream": older_zstream}

    cve_needs_internal_fix = (
        cve_eligibility_result and cve_eligibility_result.is_cve and cve_eligibility_result.needs_internal_fix
    )
    if cve_needs_internal_fix and fix_version:
        internal_branch = await _map_version_to_branch(fix_version, cve_needs_internal_fix=True)
        if internal_branch and not internal_branch.startswith("c"):
            updates["needs_internal_fix"] = True
            updates["internal_target_branch"] = internal_branch

    input_with_flag = input.model_copy(update=updates)
    return render_template("triage/prompt.j2", input_with_flag)


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
                "clone_repository",
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
            ConditionalRequirement("clone_repository", only_after=["get_jira_details"]),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
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
            "There is a firewall in place that may block some outgoing network requests "
            "(e.g. curl, wget, git clone to external hosts). If a shell command fails "
            "due to a blocked connection and the data it would provide is essential "
            "for your task, stop and report an error. Never guess or fabricate "
            "content that you were unable to retrieve.",
        ],
    )


async def run_workflow(
    jira_issue,
    dry_run,
    triage_agent_factory,
    auto_chain=False,
    force_cve_triage=False,
    user_triggered=False,
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

            dup_key = state.cve_eligibility_result.duplicate_of

            if eligibility == TriageEligibility.IMMEDIATELY:
                if dup_key and not dry_run:
                    logger.info(
                        f"Issue {state.jira_issue} has a closed/rejected duplicate "
                        f"{dup_key} — posting informational comment and proceeding"
                    )
                    try:
                        await tasks.comment_in_jira(
                            jira_issue=state.jira_issue,
                            agent_type="Triage",
                            comment_text=(
                                f"An older tracker {dup_key} exists for the same CVE, "
                                f"component, and fix version, but it was closed/rejected. "
                                f"Proceeding with triage for this tracker."
                            ),
                            available_tools=gateway_tools,
                            user_triggered=user_triggered,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to post duplicate info comment: {e}")
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
            elif dup_key:
                state.triage_result = OutputSchema(
                    resolution=Resolution.OPEN_ENDED_ANALYSIS,
                    data=OpenEndedAnalysisData(
                        summary=(
                            f"Duplicate tracker detected. {state.jira_issue} appears to be "
                            f"a duplicate of {dup_key} (same CVE, component, and fix version)."
                        ),
                        recommendation=(f"Consider closing this issue as a duplicate of {dup_key}."),
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

            # Pre-fetch JIRA details for z-stream prompt variant and summary propagation
            fix_version_name = None
            jira_details = {}
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
                logger.warning(f"Failed to pre-fetch Jira details for prompt selection: {e}")

            input_data = InputSchema(issue=state.jira_issue)
            response = await triage_agent.run(
                await render_prompt(
                    input_data,
                    fix_version=fix_version_name,
                    cve_eligibility_result=state.cve_eligibility_result,
                ),
                expected_output=render_template("triage/output_format.j2"),
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

            # Propagate Jira summary to triage data for downstream agents
            if hasattr(state.triage_result.data, "summary"):
                jira_summary = jira_details.get("fields", {}).get("summary")
                if jira_summary:
                    state.triage_result.data.summary = jira_summary

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
                if state.triage_result.resolution == Resolution.REBUILD:
                    return "verify_rebuild_buildroot"
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
                return "verify_rebuild_buildroot"
            return "comment_in_jira"

        async def verify_rebuild_buildroot(state):
            """Verify the dependency's fixed build is available in the target buildroot."""
            data = state.triage_result.data
            dep_issue_key = getattr(data, "dependency_issue", None)
            dep_component = getattr(data, "dependency_component", None)

            if not dep_issue_key or not dep_component or not state.target_branch:
                logger.info("Missing dependency info or target branch — skipping buildroot check")
                return "consolidate_rebuild_siblings"

            dep_details = await run_tool(
                "get_jira_details",
                available_tools=gateway_tools,
                issue_key=dep_issue_key,
            )
            fixed_in_build = dep_details.get("fields", {}).get(FIXED_IN_BUILD_CUSTOM_FIELD)
            if not fixed_in_build:
                logger.warning(f"Dependency {dep_issue_key} has no Fixed in Build — skipping buildroot check")
                return "consolidate_rebuild_siblings"

            # fix_version is already normalized by run_triage_analysis (e.g. rhel-9.8 → rhel-9.8.z)
            fix_version = getattr(data, "fix_version", None) or ""

            try:
                in_buildroot = await check_build_in_buildroot(
                    state.target_branch,
                    dep_component,
                    fixed_in_build,
                    fix_version=fix_version,
                )
            except Exception as e:
                logger.warning(f"Buildroot check failed for {dep_component} ({fixed_in_build}): {e}")
                return "consolidate_rebuild_siblings"

            if in_buildroot:
                return "consolidate_rebuild_siblings"

            logger.info(
                f"Dependency {dep_component} ({fixed_in_build}) not in "
                f"{state.target_branch} buildroot — postponing {state.jira_issue}"
            )
            state.triage_result = OutputSchema(
                resolution=Resolution.POSTPONED,
                data=PostponedData(
                    summary=(
                        f"Rebuild of {data.package} waiting for {dep_component} "
                        f"({fixed_in_build}) to land in {state.target_branch} buildroot"
                    ),
                    pending_issues=[dep_issue_key],
                    jira_issue=state.jira_issue,
                    package=data.package,
                    fix_version=data.fix_version,
                    cve_id=data.cve_id,
                    dependency_issue=dep_issue_key,
                    dependency_component=dep_component,
                ),
            )
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
            applicability_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / APPLICABILITY_DIR / state.jira_issue
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
            if not _should_update_jira(state.triage_result.resolution, user_triggered):
                logger.info(
                    f"Skipping Jira comment for {state.jira_issue} "
                    f"(resolution={state.triage_result.resolution.value}, not user-triggered)"
                )
                return Workflow.END
            await tasks.comment_in_jira(
                jira_issue=state.jira_issue,
                agent_type="Triage",
                comment_text=comment_text,
                available_tools=gateway_tools,
                user_triggered=user_triggered,
            )
            return Workflow.END

        workflow.add_step("check_cve_eligibility", check_cve_eligibility)
        workflow.add_step("run_triage_analysis", run_triage_analysis)
        workflow.add_step("verify_rebase_author", verify_rebase_author)
        workflow.add_step("determine_target_branch", determine_target_branch_step)
        workflow.add_step("check_cve_applicability", check_cve_applicability)
        workflow.add_step("verify_rebuild_buildroot", verify_rebuild_buildroot)
        workflow.add_step("consolidate_rebuild_siblings", consolidate_rebuild_siblings)
        workflow.add_step("comment_in_jira", comment_in_jira)

        response = await workflow.run(TriageState(jira_issue=jira_issue))
        return response.state


async def main() -> None:
    init_sentry()

    configure_logging(level=logging.INFO, buffer_size=int(os.getenv("LOG_BUFFER_SIZE", 0)))
    resolve_chat_model_override("triage")

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    auto_chain = os.getenv("AUTO_CHAIN", "true").lower() == "true"
    force_cve_triage = os.getenv("FORCE_CVE_TRIAGE", "false").lower() == "true"

    if jira_issue := os.getenv("JIRA_ISSUE", None):
        logger.info("Running in direct mode with environment variable")
        with span_processor.start_transaction(jira_issue, workflow="triage"):
            agent_factory = build_agent_factory_with_mock_repos(create_triage_agent, jira_issue)
            state = await run_workflow(
                jira_issue,
                dry_run,
                agent_factory,
                auto_chain=auto_chain,
                force_cve_triage=force_cve_triage,
            )
            logger.info(f"Direct run completed: {state.triage_result.model_dump_json(indent=4)}")
            if state.cve_eligibility_result:
                logger.info(f"CVE eligibility result: {state.cve_eligibility_result}")
            if state.target_branch:
                logger.info(f"Target branch: {state.target_branch}")
            return

    logger.info(f"Starting triage agent in queue mode (AUTO_CHAIN={'enabled' if auto_chain else 'disabled'})")
    max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", 1))
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        redis_logger.info(f"Connected to Redis, max retries set to {max_retries}")

        async def process_task(payload):
            task = Task.model_validate_json(payload)
            input = InputSchema.model_validate(task.metadata)
            current_jira_issue.set(input.issue)
            user_triggered = task.user_triggered
            logger.info(
                f"Processing triage for JIRA issue: {input.issue}, attempt: {task.attempts + 1}"
                + (" (user-triggered via ymir_todo)" if user_triggered else "")
            )
            if user_triggered and task.attempts == 0:
                sentry_sdk.metrics.count(
                    "ymir_todo.processed",
                    1,
                    attributes={"issue": input.issue},
                )

            # User-triggered runs will receive an acknowledgement comment,
            # but only after we successfully write the in-progress label to
            # avoid duplicate comments if the label write later fails.
            # post_user_ack_once persists ack_posted in task.metadata so that
            # retries do not re-post the ack after it has already been
            # delivered.

            current_labels, current_status = await tasks.get_jira_issue_metadata(input.issue)
            all_labels = JiraLabels.all_labels()
            terminal_ymir_labels = [
                label
                for label in current_labels
                if label in all_labels and label != JiraLabels.TRIAGE_IN_PROGRESS.value
            ]
            if (
                terminal_ymir_labels
                and JiraLabels.RETRY_NEEDED.value not in current_labels
                and JiraLabels.TRIAGE_IN_PROGRESS.value not in current_labels
                and not user_triggered
            ):
                logger.info(
                    f"Skipping duplicate triage for {input.issue} — "
                    f"already has labels: {terminal_ymir_labels}"
                )
                return

            if current_status in (IssueStatus.CLOSED.value, IssueStatus.DONE.value):
                logger.info(f"Skipping triage for {input.issue} — issue is already {current_status}")
                if user_triggered:
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_remove=["ymir_todo"],
                        dry_run=dry_run,
                        user_triggered=True,
                    )
                    await tasks.post_user_ack_once(
                        task,
                        input.issue,
                        "triage",
                        f"Issue is already **{current_status}** — skipping processing.",
                        user_triggered=True,
                        dry_run=dry_run,
                    )
                return

            async def retry(task, error, input=input, user_triggered=user_triggered):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {input.issue}"
                    )
                    # Preserve priority on retries: ymir_todo tasks go back to
                    # the priority queue, normal tasks to the standard one.
                    # Read from `task.user_triggered` (not the closure-captured
                    # variable) so we're robust to anything that might rebind
                    # the local in a future refactor.
                    retry_queue = (
                        RedisQueues.TRIAGE_QUEUE_TODO.value
                        if task.user_triggered
                        else RedisQueues.TRIAGE_QUEUE.value
                    )
                    await fix_await(redis.lpush(retry_queue, task.model_dump_json()))
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, moving to error list: {input.issue}"
                    )
                    try:
                        await tasks.set_jira_labels(
                            jira_issue=input.issue,
                            labels_to_add=[JiraLabels.TRIAGE_ERRORED.value],
                            labels_to_remove=[JiraLabels.TRIAGE_IN_PROGRESS.value],
                            dry_run=dry_run,
                            user_triggered=user_triggered,
                        )
                    except Exception as label_error:
                        logger.warning(f"Failed to set error labels on {input.issue}: {label_error}")
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            # ymir_triage_in_progress is the dedup anchor for the next fetcher
            # sweep. If we cannot write it, we must not proceed — otherwise the
            # fetcher will re-enqueue this issue and a second triage will run in
            # parallel. Re-queue the task as-is (task.attempts tracks triage
            # retries, not Jira-write retries — set_jira_labels already retries
            # the write internally) and skip processing this iteration.
            try:
                # Remove all ymir_* labels currently on the issue (including any
                # deprecated labels like ymir_fusa), except the in-progress anchor
                # we're about to add. This ensures cleanup of unknown/legacy labels
                # without requiring hardcoded references.
                labels_to_remove = [
                    label
                    for label in current_labels
                    if label.startswith("ymir_") and label != JiraLabels.TRIAGE_IN_PROGRESS.value
                ]
                await tasks.set_jira_labels(
                    jira_issue=input.issue,
                    labels_to_add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
                    labels_to_remove=labels_to_remove,
                    dry_run=dry_run,
                    user_triggered=user_triggered,
                    critical=True,
                )
                logger.info(f"Cleaned up existing labels for {input.issue}")
                # Post acknowledgement comment for user-triggered runs now that
                # the in-progress label write succeeded. This prevents duplicate
                # comments if the critical label write were to fail.
                await tasks.post_user_ack_once(
                    task=task,
                    jira_issue=input.issue,
                    agent_type="Triage",
                    comment_text=(
                        "Ymir picked up your request and started processing. "
                        "Results will be posted here when triage completes."
                    ),
                    user_triggered=user_triggered,
                    dry_run=dry_run,
                )
            except Exception as e:
                # Route through retry() so a permanently failing issue (deleted
                # ticket, per-issue permission error) is bounded by max_retries
                # instead of looping forever and blocking the queue. On
                # exhaustion the task lands in ERROR_LIST; the best-effort
                # ymir_triage_errored label write may also fail if Jira itself
                # is down — that's accepted (ERROR_LIST is the durable record).
                logger.error(
                    f"Could not set {JiraLabels.TRIAGE_IN_PROGRESS.value} on "
                    f"{input.issue} after retries: {e}; re-queuing to avoid duplicate triage."
                )
                error_msg = f"Failed to set in-progress label: {e}"
                await retry(task, ErrorData(details=error_msg, jira_issue=input.issue).model_dump_json())
                # Long sleep on purpose: critical-write retries already burned
                # ~7s, so we're past transient blips. Typical Jira outages last
                # minutes; cycling faster just spams the API.
                await asyncio.sleep(60)
                return

            try:
                logger.info(f"Starting triage processing for {input.issue}")
                with span_processor.start_transaction(input.issue, workflow="triage"):
                    state = await run_workflow(
                        input.issue,
                        dry_run,
                        create_triage_agent,
                        auto_chain=auto_chain,
                        force_cve_triage=input.force_cve_triage,
                        user_triggered=user_triggered,
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
                logger.info(f"Triage resolved as {output.resolution.value} for {input.issue}")

                resolution_label = _RESOLUTION_TO_LABEL.get(output.resolution)
                if resolution_label and output.resolution != Resolution.ERROR:
                    # Terminal resolution label is the dedup anchor that replaces
                    # ymir_triage_in_progress — must be written unconditionally so
                    # the next fetcher sweep skips this issue.
                    await tasks.set_jira_labels(
                        jira_issue=input.issue,
                        labels_to_add=[resolution_label.value],
                        labels_to_remove=[JiraLabels.TRIAGE_IN_PROGRESS.value],
                        dry_run=dry_run,
                        user_triggered=user_triggered,
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
                                    user_triggered=user_triggered,
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
                            downstream_payload = output.data.model_dump_json()
                        elif output.resolution == Resolution.CLARIFICATION_NEEDED:
                            # Clarification does not require a target branch.
                            task = Task(metadata=state.model_dump(), user_triggered=user_triggered)
                            queue = RedisQueues.CLARIFICATION_NEEDED_QUEUE.value
                            downstream_payload = task.model_dump_json()
                        elif not state.target_branch:
                            # Modular packages (and other unmapped tickets) return
                            # None; skip branch-based queues to avoid runtime errors.
                            logger.info(f"No target branch for {input.issue} — skipping downstream dispatch")
                            queue = None
                        else:
                            task = Task(metadata=state.model_dump(), user_triggered=user_triggered)
                            downstream_payload = task.model_dump_json()
                            if output.resolution == Resolution.REBASE:
                                queue = RedisQueues.get_rebase_queue_for_branch(
                                    state.target_branch, task.user_triggered
                                )
                            elif output.resolution == Resolution.BACKPORT:
                                queue = RedisQueues.get_backport_queue_for_branch(
                                    state.target_branch, task.user_triggered
                                )
                            else:
                                queue = RedisQueues.get_rebuild_queue_for_branch(
                                    state.target_branch, task.user_triggered
                                )
                        if queue is not None:
                            await fix_await(redis.lpush(queue, downstream_payload))
                            logger.info(f"Pushed {input.issue} to {queue}")
                    else:
                        logger.info(f"AUTO_CHAIN disabled, skipping downstream queue for {input.issue}")

        shutdown_event = asyncio.Event()
        install_shutdown_handler(asyncio.get_running_loop(), shutdown_event)
        await run_task_loop(
            redis,
            [RedisQueues.TRIAGE_QUEUE_TODO.value, RedisQueues.TRIAGE_QUEUE.value],
            process_task,
            max_concurrent=max_concurrent_tasks,
            shutdown_event=shutdown_event,
        )


if __name__ == "__main__":
    try:
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
