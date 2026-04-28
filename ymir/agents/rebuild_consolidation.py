import logging
from textwrap import dedent

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.tools import Tool
from pydantic import BaseModel, Field

from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    run_tool,
)
from ymir.common.models import (
    ConsolidatedIssue,
    CVEEligibilityResult,
    RebuildData,
    TriageEligibility,
)
from ymir.tools.privileged.jira import build_rebuild_siblings_jql

logger = logging.getLogger(__name__)


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


async def find_rebuild_siblings(
    jira_issue: str,
    rebuild_data: RebuildData,
    available_tools: list[Tool],
) -> list[ConsolidatedIssue]:
    """
    Find sibling Jira issues that can share a single rebuild MR.

    Searches for other issues against the same package and fix_version,
    then uses an LLM to verify each is a dependency rebuild with a shipped fix.

    Returns a list of confirmed sibling issues (may be empty).
    """
    if not rebuild_data.fix_version:
        logger.info(f"No fix_version for {jira_issue}, skipping consolidation")
        return []

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
        return []

    if not candidates:
        return []

    logger.info(f"Analyzing {len(candidates)} sibling candidates for {jira_issue}")

    analysis_tools = [t for t in available_tools if t.name in ["get_jira_details", "search_jira_issues"]]
    consolidated: list[ConsolidatedIssue] = []

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
                continue
        except Exception as e:
            logger.warning(f"Failed to check eligibility for sibling {candidate_key}: {e}")
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
                consolidated.append(
                    ConsolidatedIssue(
                        issue_key=candidate_key,
                        dependency_issue=analysis.dependency_issue,
                        dependency_component=analysis.dependency_component,
                    )
                )
                logger.info(
                    f"Sibling {candidate_key} confirmed as dependency rebuild "
                    f"(dependency: {analysis.dependency_component})"
                )
            else:
                logger.info(f"Sibling {candidate_key} is not a dependency rebuild")
        except Exception as e:
            logger.warning(f"Failed to analyze sibling {candidate_key}: {e}")

    if consolidated:
        logger.info(f"Consolidated {len(consolidated)} sibling(s) into rebuild for {jira_issue}")

    return consolidated


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
           on it to check if its 'Fixed in Build' field is set
           (non-null/non-empty)
        5. Set is_dependency_rebuild=true ONLY if the dependency has
           'Fixed in Build' set

        Return your analysis as JSON.""")
