import logging
from pathlib import Path
from textwrap import dedent

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.tools import Tool
from pydantic import BaseModel, Field

from ymir.agents.cve_applicability_agent import build_applicability_prompt, create_applicability_agent
from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    run_tool,
)
from ymir.common.models import (
    ApplicabilityResult,
    ConsolidatedIssue,
    CVEEligibilityResult,
    RebuildData,
    Resolution,
    TriageEligibility,
)
from ymir.common.version_utils import get_fix_version_variants

logger = logging.getLogger(__name__)


def build_rebuild_siblings_jql(
    issue_key: str,
    component: str,
    fix_version: str,
) -> str:
    escaped_component = component.replace('"', '\\"')

    variants = get_fix_version_variants(fix_version)
    quoted = ", ".join(f'"{v}"' for v in variants)
    version_clause = f"fixVersion in ({quoted})"

    return (
        f'project = RHEL AND component = "{escaped_component}" '
        f"AND {version_clause} "
        f'AND key != "{issue_key}" '
        f'AND labels = "SecurityTracking" '
        f"AND labels not in "
        f'("ymir_triaged_rebuild", "ymir_rebuilt", '
        f'"ymir_triaged_not_affected", "ymir_triaged_backport", "ymir_triaged_rebase") '
        f'AND status in ("New", "Planning")'
    )


class SiblingRebuildAnalysis(BaseModel):
    """LLM output schema for analyzing whether a sibling issue is a dependency rebuild."""

    is_dependency_rebuild: bool = Field(
        description="True if this issue requires rebuilding against an updated dependency "
        "with no source code changes needed"
    )
    dependency_issue: str | None = Field(
        default=None,
        description="Jira issue key of the dependency that needs to be rebuilt against",
    )
    dependency_component: str | None = Field(
        default=None,
        description="Component name of the dependency (e.g. 'golang', 'openssl')",
    )
    cve_id: str | None = Field(
        default=None,
        description="CVE identifier from the issue summary (e.g. 'CVE-2024-1234')",
    )


async def find_rebuild_siblings(
    jira_issue: str,
    rebuild_data: RebuildData,
    available_tools: list[Tool],
    local_clone: Path | None = None,
    unpacked_sources: Path | None = None,
    target_branch: str | None = None,
) -> tuple[list[ConsolidatedIssue], str]:
    """
    Find sibling Jira issues that can share a single rebuild MR.

    Searches for other issues against the same package and fix_version,
    then uses an LLM to verify each is a dependency rebuild with a shipped fix.
    When source clone paths are provided, runs a CVE applicability check per
    sibling and excludes those whose CVE doesn't affect the package.

    Returns (consolidated_issues, summary_text).
    """
    if not rebuild_data.fix_version:
        logger.info(f"No fix_version for {jira_issue}, skipping consolidation")
        return [], ""

    try:
        jql = build_rebuild_siblings_jql(
            issue_key=jira_issue,
            component=rebuild_data.package,
            fix_version=rebuild_data.fix_version,
        )
        candidates = await run_tool(
            "search_jira_issues",
            available_tools=available_tools,
            jql=jql,
            fields=["key", "summary"],
            max_results=50,
        )
    except Exception as e:
        logger.warning(f"Failed to find rebuild siblings for {jira_issue}: {e}")
        return [], ""

    if not candidates:
        return [], ""

    logger.info(f"Analyzing {len(candidates)} sibling candidates for {jira_issue}")

    analysis_tools = [t for t in available_tools if t.name in ["get_jira_details", "search_jira_issues"]]
    consolidated: list[ConsolidatedIssue] = []
    summary_lines: list[str] = []

    for candidate in candidates:
        candidate_key = candidate.get("key", "")
        try:
            eligibility_result = CVEEligibilityResult.model_validate(
                await run_tool(
                    "check_cve_triage_eligibility",
                    available_tools=available_tools,
                    issue_key=candidate_key,
                )
            )
            if eligibility_result.eligibility != TriageEligibility.IMMEDIATELY:
                logger.info(f"Sibling {candidate_key} not eligible: {eligibility_result.reason}")
                summary_lines.append(
                    f"* {candidate_key} — excluded (not eligible: {eligibility_result.reason})"
                )
                continue
        except Exception as e:
            logger.warning(f"Failed to check eligibility for sibling {candidate_key}: {e}")
            summary_lines.append(f"* {candidate_key} — excluded (eligibility check failed)")
            continue

        try:
            analysis_agent = RequirementAgent(
                name="SiblingRebuildAnalyzer",
                llm=get_chat_model(),
                tool_call_checker=get_tool_call_checker_config(),
                tools=analysis_tools,
                memory=UnconstrainedMemory(),
            )
            prompt = _build_sibling_analysis_prompt(
                candidate_key=candidate_key,
                jira_issue=jira_issue,
                package=rebuild_data.package,
                dependency_component=rebuild_data.dependency_component,
            )
            response = await analysis_agent.run(
                prompt,
                expected_output=SiblingRebuildAnalysis,
                **get_agent_execution_config(),
            )
            analysis = SiblingRebuildAnalysis.model_validate_json(response.last_message.text)

            if analysis.is_dependency_rebuild:
                logger.info(
                    f"Sibling {candidate_key} confirmed as dependency rebuild "
                    f"(dependency: {analysis.dependency_component})"
                )
                cve_id = analysis.cve_id

                if (
                    local_clone
                    and unpacked_sources
                    and target_branch
                    and cve_id
                    and not await _check_sibling_applicability(
                        candidate_key=candidate_key,
                        cve_id=cve_id,
                        package=rebuild_data.package,
                        target_branch=target_branch,
                        dep_component=analysis.dependency_component,
                        dep_issue_key=analysis.dependency_issue,
                        local_clone=local_clone,
                        unpacked_sources=unpacked_sources,
                        available_tools=available_tools,
                    )
                ):
                    summary_lines.append(
                        f"* {candidate_key} — excluded ({cve_id} does not affect {rebuild_data.package})"
                    )
                    continue

                dep_parts = []
                if analysis.dependency_component:
                    dep_parts.append(analysis.dependency_component)
                if analysis.dependency_issue:
                    dep_parts.append(analysis.dependency_issue)
                dep_info = f" (dependency: {', '.join(dep_parts)})" if dep_parts else ""
                cve_info = f" [{cve_id}]" if cve_id else ""
                summary_lines.append(f"* {candidate_key}{cve_info}{dep_info} — included")
                consolidated.append(
                    ConsolidatedIssue(
                        issue_key=candidate_key,
                        dependency_issue=analysis.dependency_issue,
                        dependency_component=analysis.dependency_component,
                    )
                )
            else:
                logger.info(f"Sibling {candidate_key} is not a dependency rebuild")
                summary_lines.append(f"* {candidate_key} — excluded (not a dependency rebuild)")
        except Exception as e:
            logger.warning(f"Failed to analyze sibling {candidate_key}: {e}")
            summary_lines.append(f"* {candidate_key} — excluded (analysis failed)")

    if consolidated:
        logger.info(f"Consolidated {len(consolidated)} sibling(s) into rebuild for {jira_issue}")

    return consolidated, "\n".join(summary_lines)


def _build_sibling_analysis_prompt(
    candidate_key: str,
    jira_issue: str,
    package: str,
    dependency_component: str | None,
) -> str:
    dep_context = f" against updated dependency '{dependency_component}'" if dependency_component else ""
    return dedent(f"""\
        Analyze Jira issue {candidate_key} to determine if it requires
        a dependency rebuild.

        Context: Package '{package}' has issue {jira_issue} which requires
        a rebuild{dep_context}. We are checking if sibling issue
        {candidate_key} also requires a dependency rebuild.

        Steps:
        1. Use get_jira_details to examine issue {candidate_key}
        2. Determine if this issue requires the package to be rebuilt
           against an updated dependency (no source code changes needed
           for the package itself)
        3. If yes, find the dependency issue:
           - Check issuelinks for linked issues with a different
             component than '{package}'
           - If not found via issuelinks, extract the CVE ID from
             the summary and use search_jira_issues with JQL:
             project = RHEL AND summary ~ "<CVE-ID>" \
        AND component != "{package}"
        4. Once you find the dependency issue, use get_jira_details
           on it and thoroughly verify it was actually fixed:
           - Check if 'Fixed in Build' field is set (non-null/non-empty)
           - Check the issue status and resolution — if the dependency
             issue was Closed/Done with resolution like 'NOTABUG',
             'WONTFIX', 'DUPLICATE', 'CANTFIX', or 'DROPPED',
             the fix was never actually built and the rebuild is
             not needed
           - Only consider the dependency as fixed if it has
             'Fixed in Build' set AND was not dropped/rejected
        5. Set is_dependency_rebuild=true ONLY if the dependency was
           genuinely fixed (has 'Fixed in Build' and was not
           dropped/rejected)
        6. Extract the CVE ID from the issue summary (e.g. CVE-2024-1234)

        Return your analysis as JSON.""")


async def _check_sibling_applicability(
    *,
    candidate_key: str,
    cve_id: str,
    package: str,
    target_branch: str,
    dep_component: str | None,
    dep_issue_key: str | None,
    local_clone: Path,
    unpacked_sources: Path,
    available_tools: list[Tool],
) -> bool:
    """Run CVE applicability check for a sibling issue. Returns True if affected/inconclusive."""
    logger.info(f"Running applicability check for sibling {candidate_key} ({cve_id})")
    try:
        local_tool_options = {"working_directory": local_clone}
        agent = create_applicability_agent(available_tools, local_tool_options)
        prompt = build_applicability_prompt(
            jira_issue=candidate_key,
            package=package,
            target_branch=target_branch,
            resolution=Resolution.REBUILD,
            cve_id=cve_id,
            dep_component=dep_component,
            dep_issue_key=dep_issue_key,
            patch_files=[],
            unpacked_sources=unpacked_sources,
            local_clone=local_clone,
        )
        response = await agent.run(
            prompt,
            expected_output=ApplicabilityResult,
            **get_agent_execution_config(),
        )
        result = ApplicabilityResult.model_validate_json(response.last_message.text)

        if not result.is_affected:
            logger.info(f"Sibling {candidate_key} CVE not applicable: {result.justification_category}")
            return False

        logger.info(f"Sibling {candidate_key} CVE confirmed applicable")
        return True
    except Exception as e:
        logger.warning(f"Applicability check failed for sibling {candidate_key}: {e}")
        return True
