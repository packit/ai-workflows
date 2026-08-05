import asyncio
import logging
import os
from textwrap import dedent

from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.tools import Tool
from pydantic import BaseModel, Field, model_validator

import ymir.agents.tasks as tasks
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    run_tool,
)
from ymir.common.base_utils import fix_await, redis_client
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import (
    ConsolidatedIssue,
    CVEEligibilityResult,
    RebaseData,
    Task,
    TriageEligibility,
)
from ymir.common.version_utils import compare_versions_async, get_fix_version_variants

logger = logging.getLogger(__name__)


def build_siblings_jql(
    issue_key: str,
    component: str,
    fix_version: str,
    excluded_labels: list[str],
) -> str:
    """
    Build JQL query to find sibling issues for consolidation.

    Args:
        issue_key: Primary issue key to exclude from results
        component: Package component name
        fix_version: Fix version to match (supports variants)
        excluded_labels: Jira labels to exclude (e.g., terminal triage labels)

    Returns:
        JQL query string
    """
    escaped_component = component.replace('"', '\\"')

    variants = get_fix_version_variants(fix_version)
    quoted = ", ".join(f'"{v}"' for v in variants)
    version_clause = f"fixVersion in ({quoted})"

    # Build base query
    jql = (
        f'project = RHEL AND component = "{escaped_component}" '
        f"AND {version_clause} "
        f'AND key != "{issue_key}" '
        f'AND labels = "SecurityTracking" '
    )

    # Only add label exclusion clause if there are labels to exclude
    if excluded_labels:
        excluded = ", ".join(f'"{label}"' for label in excluded_labels)
        jql += f"AND labels not in ({excluded}) "

    jql += 'AND status in ("New", "Planning")'
    return jql


def build_rebase_siblings_jql(
    issue_key: str,
    component: str,
    fix_version: str,
) -> str:
    """Build JQL query to find rebase sibling candidates."""
    return build_siblings_jql(
        issue_key=issue_key,
        component=component,
        fix_version=fix_version,
        excluded_labels=[
            JiraLabels.TRIAGED_NOT_AFFECTED.value,
            JiraLabels.TRIAGED_BACKPORT.value,
            JiraLabels.TRIAGED_REBUILD.value,
            JiraLabels.TRIAGED_REBASE.value,
            JiraLabels.TRIAGED_POSTPONED.value,
        ],
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

    @model_validator(mode="after")
    def validate_target_version(self):
        """Validate that target_version is present when requires_same_rebase is True."""
        if self.requires_same_rebase and not self.target_version:
            raise ValueError("target_version must be provided when requires_same_rebase is True")
        return self


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

    # Limit concurrent sibling analyses to avoid overwhelming downstream services
    semaphore = asyncio.Semaphore(10)

    async def analyze_candidate(candidate: dict) -> tuple[ConsolidatedIssue | None, str]:
        """Analyze a single candidate sibling for consolidation eligibility."""
        async with semaphore:
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
                    return None, f"* {candidate_key} — excluded (not eligible: {eligibility_result.reason})"
            except Exception as e:
                logger.warning(f"Failed to check eligibility for sibling {candidate_key}: {e}")
                return None, f"* {candidate_key} — excluded (eligibility check failed)"

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
                    # Guard: target_version must be present when requires_same_rebase is True
                    if not analysis.target_version:
                        logger.warning(
                            f"Sibling {candidate_key} marked as requires_same_rebase "
                            "but missing target_version"
                        )
                        return (
                            None,
                            f"* {candidate_key} — excluded (missing target version)",
                        )
                    cmp_result = await compare_versions_async(analysis.target_version, rebase_data.version)
                    if cmp_result == 0:
                        logger.info(
                            f"Sibling {candidate_key} confirmed as requiring rebase "
                            f"to {analysis.target_version}"
                        )
                        cve_id = analysis.cve_id
                        cve_info = f" [{cve_id}]" if cve_id else ""
                        consolidated_issue = ConsolidatedIssue(
                            issue_key=candidate_key,
                            dependency_issue=None,
                            dependency_component=None,
                        )
                        return (
                            consolidated_issue,
                            f"* {candidate_key}{cve_info} — included (target: {analysis.target_version})",
                        )
                    logger.info(
                        f"Sibling {candidate_key} requires different version: "
                        f"{analysis.target_version} != {rebase_data.version}"
                    )
                    return (
                        None,
                        f"* {candidate_key} — excluded (different target version: {analysis.target_version})",
                    )
                logger.info(f"Sibling {candidate_key} does not require a rebase")
                return None, f"* {candidate_key} — excluded (not a rebase)"
            except Exception as e:
                logger.warning(f"Failed to analyze sibling {candidate_key}: {e}")
                return None, f"* {candidate_key} — excluded (analysis failed)"

    # Analyze all candidates in parallel
    results = await asyncio.gather(*[analyze_candidate(c) for c in candidates])

    # Collect consolidated issues and summary lines
    consolidated: list[ConsolidatedIssue] = []
    summary_lines: list[str] = []
    for issue, summary_line in results:
        if issue:
            consolidated.append(issue)
        summary_lines.append(summary_line)

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


async def queue_siblings_for_triage(
    primary_issue: str,
    rebase_data: RebaseData,
    available_tools: list[Tool],
    dry_run: bool = False,
    user_triggered: bool = False,
) -> int:
    """
    Queue sibling issues for triage and mark primary as waiting.

    Finds sibling candidates, checks eligibility, queues eligible siblings for triage,
    and marks both siblings and primary with appropriate labels.

    Args:
        primary_issue: Primary issue key
        rebase_data: Rebase data containing package and fix_version
        available_tools: Available tools for Jira operations
        dry_run: If True, skip all mutations (Redis, Jira labels, comments)
        user_triggered: Whether this was triggered by user action

    Returns:
        Number of siblings queued for triage (or would be queued in dry-run)
    """
    if not rebase_data.fix_version:
        logger.info(f"No fix_version for {primary_issue}, skipping sibling queue")
        return 0

    try:
        jql = build_rebase_siblings_jql(
            issue_key=primary_issue,
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
        logger.warning(f"Failed to find sibling candidates for {primary_issue}: {e}")
        return 0

    if not candidates:
        logger.info(f"No sibling candidates found for {primary_issue}")
        return 0

    logger.info(f"Found {len(candidates)} sibling candidates for {primary_issue}")

    queued_count = 0
    for candidate in candidates:
        candidate_key = candidate.get("key", "")
        if not candidate_key:
            continue

        try:
            # Check eligibility
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

            # Queue for triage (skip in dry-run)
            if not dry_run:
                task = Task.from_issue(candidate_key)
                async with redis_client(os.environ["REDIS_URL"]) as redis:
                    await fix_await(redis.lpush(RedisQueues.TRIAGE_QUEUE.value, task.model_dump_json()))

            # Increment count immediately after Redis push (or would-push in dry-run)
            # so that failures in label/comment don't miscount queued siblings
            queued_count += 1
            logger.info(
                f"{'[DRY-RUN] Would queue' if dry_run else 'Queued'} sibling {candidate_key} for triage"
            )

            if not dry_run:
                # Add label (best-effort; failure doesn't affect queue count)
                try:
                    await tasks.set_jira_labels(
                        jira_issue=candidate_key,
                        labels_to_add=[JiraLabels.REBASE_SIBLING.value],
                        dry_run=False,
                        user_triggered=user_triggered,
                    )
                except Exception as e:
                    logger.warning(f"Failed to label sibling {candidate_key}: {e}")

                # Post comment on sibling (best-effort; failure doesn't affect queue count)
                try:
                    await tasks.comment_in_jira(
                        jira_issue=candidate_key,
                        agent_type="Triage",
                        comment_text=f"Queued for triage as potential sibling of {primary_issue}",
                        available_tools=available_tools,
                        is_error=False,
                        user_triggered=user_triggered,
                    )
                except Exception as e:
                    logger.warning(f"Failed to comment on sibling {candidate_key}: {e}")

        except Exception as e:
            import traceback

            logger.warning(
                f"Failed to queue sibling {candidate_key}: {e}\nTraceback: {traceback.format_exc()}"
            )
            continue

    if queued_count > 0 and not dry_run:
        # Add label to primary
        try:
            await tasks.set_jira_labels(
                jira_issue=primary_issue,
                labels_to_add=[JiraLabels.WAITING_FOR_SIBLINGS.value],
                dry_run=False,
                user_triggered=user_triggered,
            )
        except Exception as e:
            logger.warning(f"Failed to label primary {primary_issue} as waiting: {e}")

        # Post comment on primary
        try:
            comment_text = f"Waiting for {queued_count} sibling(s) to finish triaging before starting rebase"
            await tasks.comment_in_jira(
                jira_issue=primary_issue,
                agent_type="Triage",
                comment_text=comment_text,
                available_tools=available_tools,
                is_error=False,
                user_triggered=user_triggered,
            )
        except Exception as e:
            logger.warning(f"Failed to comment on primary {primary_issue}: {e}")

        logger.info(f"Queued {queued_count} siblings for {primary_issue}")

    return queued_count


async def check_and_queue_primary_if_ready(
    sibling_issue: str,
    available_tools: list[Tool],
    dry_run: bool = False,
    user_triggered: bool = False,
) -> None:
    """
    Check if all siblings are done triaging and queue primary if ready.

    When a sibling finishes triaging, this function:
    1. Extracts the primary issue from the sibling's comments
    2. Checks if all siblings have finished triaging
    3. If all done: queues primary for rebase and removes waiting label

    Args:
        sibling_issue: Sibling issue that just finished triaging
        available_tools: Available tools for Jira operations
        dry_run: If True, skip all mutations (Redis, Jira labels)
        user_triggered: Whether this was triggered by user action
    """
    try:
        # Get sibling's details including comments to find primary issue
        sibling_details = await run_tool(
            "get_jira_details",
            available_tools=available_tools,
            issue_key=sibling_issue,
        )

        # Look for comment matching "Queued for triage as potential sibling of {primary}"
        primary_issue = None
        comments = sibling_details.get("comments", [])
        for comment in comments:
            body = comment.get("body", "")
            if "Queued for triage as potential sibling of" in body:
                # Extract issue key (format: RHEL-123456)
                import re

                match = re.search(r"RHEL-\d+", body)
                if match:
                    primary_issue = match.group(0)
                    break

        if not primary_issue:
            logger.info(f"No primary issue found in {sibling_issue} comments, skipping check")
            return

        logger.info(f"Found primary issue {primary_issue} for sibling {sibling_issue}")

        # Check if primary is still waiting for siblings
        primary_details = await run_tool(
            "get_jira_details",
            available_tools=available_tools,
            issue_key=primary_issue,
        )
        labels = primary_details.get("labels", [])
        if JiraLabels.WAITING_FOR_SIBLINGS.value not in labels:
            logger.info(f"Primary {primary_issue} is not waiting for siblings, skipping")
            return

        # Check if any siblings are still pending (have ymir_rebase_sibling label)
        # Extract component and fix_version from primary to search for its specific siblings
        fields = primary_details.get("fields", {})
        components = fields.get("components", [])
        component = components[0].get("name") if components else None
        fix_versions = fields.get("fixVersions", [])
        fix_version = fix_versions[0].get("name") if fix_versions else None

        if not component or not fix_version:
            logger.warning(
                f"Primary {primary_issue} missing component or fix_version, cannot check for pending siblings"
            )
            return

        # Use the same JQL builder to ensure we only get siblings of THIS primary
        jql = build_rebase_siblings_jql(
            issue_key=primary_issue,
            component=component,
            fix_version=fix_version,
        )
        # Add filter for ymir_rebase_sibling label
        jql_with_label = f'{jql} AND labels = "{JiraLabels.REBASE_SIBLING.value}"'

        pending_siblings = await run_tool(
            "search_jira_issues",
            available_tools=available_tools,
            jql=jql_with_label,
            fields=["key"],
            max_results=1,  # We just need to know if any exist
        )

        if pending_siblings:
            logger.info(f"Primary {primary_issue} still has pending siblings, not ready to queue")
            return

        # All siblings are done! Queue primary for rebase
        logger.info(
            f"All siblings done for {primary_issue}, "
            f"{'[DRY-RUN] would queue' if dry_run else 'queueing'} for rebase"
        )

        if not dry_run:
            # Remove waiting label
            await tasks.set_jira_labels(
                jira_issue=primary_issue,
                labels_to_remove=[JiraLabels.WAITING_FOR_SIBLINGS.value],
                dry_run=False,
                user_triggered=user_triggered,
            )

            # Re-queue primary to triage queue
            # Triage will see the existing ymir_triaged_rebase label, skip expensive analysis,
            # and queue for rebase with proper full state (Task.metadata = state.model_dump())
            task = Task.from_issue(primary_issue, user_triggered=user_triggered)
            async with redis_client(os.environ["REDIS_URL"]) as redis:
                await fix_await(redis.lpush(RedisQueues.TRIAGE_QUEUE.value, task.model_dump_json()))
            logger.info(f"Re-queued {primary_issue} to triage (will queue to rebase with full state)")

        # Post comment
        await tasks.comment_in_jira(
            jira_issue=primary_issue,
            agent_type="Triage",
            comment_text="All siblings have finished triaging, starting rebase",
            available_tools=available_tools,
            is_error=False,
            user_triggered=False,
        )

        logger.info(f"Queued primary {primary_issue} for rebase")

    except Exception as e:
        logger.warning(f"Failed to check and queue primary for {sibling_issue}: {e}")


async def find_triaged_rebase_siblings(
    jira_issue: str,
    rebase_data: RebaseData,
    available_tools: list[Tool],
) -> tuple[list[ConsolidatedIssue], str]:
    """
    Find siblings that have already been triaged as REBASE to the same version.

    This function is called when the primary issue's rebase workflow starts.
    It searches for siblings with ymir_triaged_rebase label, then checks their
    comments for the reference to the primary issue.

    Returns (consolidated_issues, summary_text).
    """
    if not rebase_data.fix_version:
        logger.info(f"No fix_version for {jira_issue}, skipping consolidation")
        return [], ""

    try:
        # Build JQL to find siblings with ymir_triaged_rebase
        jql = build_siblings_jql(
            issue_key=jira_issue,
            component=rebase_data.package,
            fix_version=rebase_data.fix_version,
            excluded_labels=[],  # Don't exclude any labels
        )
        # Add filter for ymir_triaged_rebase label
        jql_with_label = f'{jql} AND labels = "{JiraLabels.TRIAGED_REBASE.value}"'

        candidates = await run_tool(
            "search_jira_issues",
            available_tools=available_tools,
            jql=jql_with_label,
            fields=["key", "summary"],
            max_results=50,
        )
    except Exception as e:
        logger.warning(f"Failed to find triaged siblings for {jira_issue}: {e}")
        return [], ""

    if not candidates:
        logger.info(f"No triaged sibling candidates found for {jira_issue}")
        return [], ""

    logger.info(f"Found {len(candidates)} triaged sibling candidates for {jira_issue}")

    # Limit concurrent sibling checks to avoid overwhelming downstream services
    semaphore = asyncio.Semaphore(10)

    async def check_candidate(candidate: dict) -> tuple[ConsolidatedIssue | None, str]:
        """Check if a triaged sibling has comment referencing the primary issue."""
        async with semaphore:
            candidate_key = candidate.get("key", "")
            try:
                # Get sibling's details including comments to check for primary issue reference
                candidate_details = await run_tool(
                    "get_jira_details",
                    available_tools=available_tools,
                    issue_key=candidate_key,
                )

                # Look for comment matching "Queued for triage as potential sibling of {jira_issue}"
                has_primary_reference = False
                comments = candidate_details.get("comments", [])
                for comment in comments:
                    body = comment.get("body", "")
                    if f"Queued for triage as potential sibling of {jira_issue}" in body:
                        has_primary_reference = True
                        break

                if has_primary_reference:
                    logger.info(f"Sibling {candidate_key} confirmed as sibling of {jira_issue}")
                    consolidated_issue = ConsolidatedIssue(
                        issue_key=candidate_key,
                        dependency_issue=None,
                        dependency_component=None,
                    )
                    return (
                        consolidated_issue,
                        f"* {candidate_key} — included (sibling of {jira_issue})",
                    )
                logger.info(f"Sibling {candidate_key} does not reference {jira_issue} in comments")
                return (
                    None,
                    f"* {candidate_key} — excluded (not a sibling of {jira_issue})",
                )
            except Exception as e:
                logger.warning(f"Failed to check sibling {candidate_key}: {e}")
                return None, f"* {candidate_key} — excluded (check failed)"

    # Check all candidates in parallel
    results = await asyncio.gather(*[check_candidate(c) for c in candidates])

    # Collect consolidated issues and summary lines
    consolidated: list[ConsolidatedIssue] = []
    summary_lines: list[str] = []
    for issue, summary_line in results:
        if issue:
            consolidated.append(issue)
        summary_lines.append(summary_line)

    if consolidated:
        logger.info(f"Consolidated {len(consolidated)} triaged sibling(s) for {jira_issue}")

    return consolidated, "\n".join(summary_lines)
