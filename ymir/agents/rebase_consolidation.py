import logging
from textwrap import dedent

from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.tools import Tool
from pydantic import BaseModel, Field

from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    run_tool,
)
from ymir.common.models import (
    ConsolidatedIssue,
    CVEEligibilityResult,
    RebaseData,
    TriageEligibility,
)
from ymir.common.version_utils import compare_versions, get_fix_version_variants

logger = logging.getLogger(__name__)


def build_rebase_siblings_jql(
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
        f'("ymir_triaged_not_affected", "ymir_triaged_backport", "ymir_triaged_rebuild", '
        f'"ymir_triaged_rebase", "ymir_triaged_postponed") '
        f'AND status in ("New", "Planning")'
    )


class SiblingRebaseAnalysis(BaseModel):
    """LLM output schema for analyzing whether a sibling issue requires the same rebase."""

    requires_same_rebase: bool = Field(
        description="True if this issue requires rebasing to the exact same upstream version"
    )
    target_version: str | None = Field(
        default=None,
        description="The upstream version this issue needs to be rebased to (e.g., '2.4.1')",
    )
    cve_id: str | None = Field(
        default=None,
        description="CVE identifier(s) from the issue summary (e.g. 'CVE-2024-1234'); "
        "include ALL CVE IDs when the issue covers multiple CVEs",
    )


async def find_rebase_siblings(
    jira_issue: str,
    rebase_data: RebaseData,
    available_tools: list[Tool],
) -> tuple[list[ConsolidatedIssue], str]:
    """
    Find sibling Jira issues that can share a single rebase MR.

    Searches for other issues against the same package and fix_version,
    then uses an LLM to verify each requires rebasing to the same target version.

    Returns (consolidated_issues, summary_text).
    """
    if not rebase_data.fix_version:
        logger.info(f"No fix_version for {jira_issue}, skipping consolidation")
        return [], ""

    try:
        jql = build_rebase_siblings_jql(
            issue_key=jira_issue,
            component=rebase_data.package,
            fix_version=rebase_data.fix_version,
        )
        candidates = await run_tool(
            "search_jira_issues",
            available_tools=available_tools,
            jql=jql,
            fields=["key", "summary"],
            max_results=50,
        )
    except Exception as e:
        logger.warning(f"Failed to find rebase siblings for {jira_issue}: {e}")
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
            analysis_agent = ReasoningAgent(
                name="SiblingRebaseAnalyzer",
                llm=get_chat_model(),
                unconstrained=is_reasoning_enabled(),
                tool_call_checker=get_tool_call_checker_config(),
                tools=analysis_tools,
                memory=UnconstrainedMemory(),
            )
            prompt = _build_sibling_analysis_prompt(
                candidate_key=candidate_key,
                jira_issue=jira_issue,
                package=rebase_data.package,
                target_version=rebase_data.version,
            )
            response = await analysis_agent.run(
                prompt,
                expected_output=SiblingRebaseAnalysis,
                **get_agent_execution_config(),
            )
            analysis = SiblingRebaseAnalysis.model_validate_json(response.last_message.text)

            if analysis.requires_same_rebase:
                cmp_result = compare_versions(analysis.target_version, rebase_data.version)
                if cmp_result == 0:
                    logger.info(
                        f"Sibling {candidate_key} confirmed as requiring rebase to {analysis.target_version}"
                    )
                    cve_id = analysis.cve_id
                    cve_info = f" [{cve_id}]" if cve_id else ""
                    summary_lines.append(
                        f"* {candidate_key}{cve_info} — included (target: {analysis.target_version})"
                    )
                    consolidated.append(
                        ConsolidatedIssue(
                            issue_key=candidate_key,
                            dependency_issue=None,
                            dependency_component=None,
                        )
                    )
                else:
                    logger.info(
                        f"Sibling {candidate_key} requires different version: "
                        f"{analysis.target_version} != {rebase_data.version}"
                    )
                    summary_lines.append(
                        f"* {candidate_key} — excluded (different target version: {analysis.target_version})"
                    )
            else:
                logger.info(f"Sibling {candidate_key} does not require a rebase")
                summary_lines.append(f"* {candidate_key} — excluded (not a rebase)")
        except Exception as e:
            logger.warning(f"Failed to analyze sibling {candidate_key}: {e}")
            summary_lines.append(f"* {candidate_key} — excluded (analysis failed)")

    if consolidated:
        logger.info(f"Consolidated {len(consolidated)} sibling(s) into rebase for {jira_issue}")

    return consolidated, "\n".join(summary_lines)


def _build_sibling_analysis_prompt(
    candidate_key: str,
    jira_issue: str,
    package: str,
    target_version: str,
) -> str:
    return dedent(f"""\
        Analyze Jira issue {candidate_key} to determine if it requires
        rebasing package '{package}' to version {target_version}.

        Context: Package '{package}' has issue {jira_issue} which requires
        a rebase to version {target_version}. We are checking if sibling issue
        {candidate_key} also requires rebasing to the exact same version.

        Steps:
        1. Use get_jira_details to examine issue {candidate_key}
        2. Determine if this issue requires rebasing '{package}' to
           a specific upstream version (typically to fix a CVE)
        3. If yes, identify the exact target version from:
           - Issue description or comments
           - Upstream advisory references
           - CVE database information
        4. Extract CVE ID(s) from the issue summary (e.g. CVE-2024-1234).
           Note: the summary may contain multiple CVE IDs — include all of them.
        5. Set requires_same_rebase=true ONLY if the target version
           matches exactly: {target_version}

        Return your analysis as JSON.""")
