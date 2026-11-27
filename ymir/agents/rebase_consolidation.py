import asyncio
import logging
import re
from pathlib import Path

from beeai_framework.tools import Tool
from pydantic import BaseModel, Field, model_validator
from specfile import Specfile

import ymir.agents.tasks as tasks
from ymir.agents.utils import (
    run_tool,
)
from ymir.common.constants import JiraLabels
from ymir.common.cve import extract_cve_ids
from ymir.common.models import ConsolidatedIssue, RebaseData
from ymir.common.temporal_triage import TriageIntent, connect_triage_ingress, submit_triage_intent
from ymir.common.utils import extract_text_from_adf
from ymir.common.version_utils import get_fix_version_variants

logger = logging.getLogger(__name__)


# Prefix used to read the permanent Jira comments that linked legacy rebase siblings to a primary.
SIBLING_MARKER_PREFIX = "Queued for triage as potential sibling of"


def comment_marks_sibling_of(body: str, primary_issue: str) -> bool:
    """Whether a comment body is a sibling marker naming ``primary_issue``."""
    _, marker, reference = body.partition(SIBLING_MARKER_PREFIX)
    # Jira may turn the issue key into an ADF inlineCard or HTML smartlink.
    return bool(marker) and bool(re.search(rf"\b{re.escape(primary_issue)}\b", reference))


def is_marked_sibling_of(details: dict, primary_issue: str) -> bool:
    """Whether any comment on the issue marks it as a sibling of ``primary_issue``."""
    comments = details.get("fields", {}).get("comment", {}).get("comments", [])
    return any(
        comment_marks_sibling_of(extract_text_from_adf(comment.get("body", "")), primary_issue)
        for comment in comments
    )


def get_rebase_primary_issue(details: dict) -> str | None:
    """Read the primary issue from the most recent sibling marker comment.

    The marker is a permanent comment, so an issue can carry one per queueing
    primary. The newest marker is the current relationship; an older one may
    point at a primary that already finished, and treating that as stale would
    make a re-queued sibling run as a primary and queue its own primary.
    """
    comments = details.get("fields", {}).get("comment", {}).get("comments", [])
    for comment in reversed(comments):
        body = extract_text_from_adf(comment.get("body", ""))
        _, marker, reference = body.partition(SIBLING_MARKER_PREFIX)
        # Jira may turn the issue key into an ADF inlineCard or HTML smartlink.
        if marker and (match := re.search(r"\bRHEL-\d+\b", reference)):
            return match.group(0)
    return None


_CLOSED_ISSUE_STATUSES = frozenset({"Closed", "Done", "Resolved"})
# A primary carrying one of these has stopped: it either produced a fix, failed
# permanently, resolved into a non-rebase path, or deferred the fix to a later
# explicit request. None of them will pick the sibling up, so a link to them is
# stale.
_FINISHED_PRIMARY_LABELS = frozenset(
    {
        JiraLabels.REBASED.value,
        JiraLabels.REBASE_ERRORED.value,
        JiraLabels.NEEDS_ATTENTION.value,
        JiraLabels.TRIAGE_ERRORED.value,
        JiraLabels.TRIAGED.value,
        JiraLabels.TRIAGED_BACKPORT.value,
        JiraLabels.TRIAGED_REBUILD.value,
        JiraLabels.TRIAGED_NOT_AFFECTED.value,
        *JiraLabels.deferred_triage_labels(),
    }
)


async def sibling_link_is_stale(primary_issue: str, available_tools: list[Tool]) -> bool:
    """Return whether a sibling link points at a primary that is already finished.

    The sibling marker is a permanent Jira comment, so without this check a later
    explicit ``ymir_todo`` on a former sibling would defer to a primary that
    already produced its fix and never run its own.

    Raises if the primary cannot be read: the caller decides whether that means
    "keep deferring" or "fail the run" for the request that triggered it.
    """
    primary_details = await run_tool(
        "get_jira_details",
        available_tools=available_tools,
        issue_key=primary_issue,
    )
    fields = primary_details.get("fields", {})
    status = fields.get("status", {}).get("name", "")
    labels = fields.get("labels", [])
    return status in _CLOSED_ISSUE_STATUSES or bool(_FINISHED_PRIMARY_LABELS.intersection(labels))


def uses_autochangelog(spec_path: Path) -> bool:
    """Return whether the spec delegates changelog generation to rpmautospec."""
    with Specfile(spec_path) as spec:
        return spec.has_autochangelog


def changelog_entry_headers(spec_path: Path) -> list[str]:
    """Return RPM changelog headers in newest-to-oldest order."""
    with Specfile(spec_path) as spec, spec.changelog() as changelog:
        return [entry.header for entry in reversed(changelog)] if changelog else []


def has_new_latest_changelog_entry(spec_path: Path, previous_headers: list[str]) -> bool:
    """Return whether a new entry was prepended without replacing historical entries."""
    headers = changelog_entry_headers(spec_path)
    return len(headers) > len(previous_headers) and headers[1:] == previous_headers


def add_jira_tickets_to_latest_changelog_entry(
    spec_path: Path,
    jira_issues: list[str],
    previous_headers: list[str] | None = None,
) -> bool:
    """Add missing Jira Resolves lines to the newest spec changelog entry.

    Returns ``True`` only when the spec file was changed.  A spec with an empty
    ``%changelog`` section has no entry to update, so it is deliberately left
    untouched.
    """
    if previous_headers is not None and not has_new_latest_changelog_entry(spec_path, previous_headers):
        raise ValueError("A new changelog entry is required before adding consolidation metadata")

    with Specfile(spec_path) as spec, spec.changelog() as changelog:
        if not changelog:
            return False

        entry = changelog[-1]
        references = [
            reference for line in entry.content if (reference := tasks.parse_changelog_reference(line))
        ]
        resolves_references = [
            reference for reference in references if reference.tag.casefold() == "resolves"
        ]
        suffix_lines = [
            (index, suffix)
            for index, line in enumerate(entry.content)
            if not tasks.is_changelog_reference_line(line) and (suffix := tasks._extract_jira_suffix(line))
        ]
        existing_issues = {
            issue.upper()
            for reference in resolves_references
            for issue in re.findall(r"\bRHEL-\d+\b", reference.line, re.IGNORECASE)
        }
        existing_issues.update(
            issue.upper()
            for _, suffix in suffix_lines
            for issue in re.findall(r"\bRHEL-\d+\b", suffix, re.IGNORECASE)
        )
        missing_issues = [
            issue for issue in dict.fromkeys(jira_issues) if issue.upper() not in existing_issues
        ]
        if not missing_issues:
            return False

        inline_references = [reference for reference in resolves_references if reference.is_inline]
        if inline_references:
            reference = inline_references[0]
            index = next(
                index
                for index, line in enumerate(entry.content)
                if tasks.parse_changelog_reference(line) == reference
            )
            entry.content[index] = reference.render(issues=[*reference.issues, *missing_issues])
            return True

        if suffix_lines:
            index, suffix = suffix_lines[0]
            line = entry.content[index]
            suffix_issues = re.findall(r"\bRHEL-\d+\b", suffix, re.IGNORECASE)
            suffix_issues.extend(issue for issue in missing_issues if issue.upper() not in existing_issues)
            suffix_start = line.rfind(suffix)
            entry.content[index] = (
                line[:suffix_start]
                + tasks._rewrite_jira_suffix(suffix, suffix_issues)
                + line[suffix_start + len(suffix) :]
            )
            return True

        reference_prefix = resolves_references[0].prefix if resolves_references else "- "
        reference_tag = resolves_references[0].tag if resolves_references else "Resolves"
        entry.content.extend(f"{reference_prefix}{reference_tag}: {issue}" for issue in missing_issues)
        return True


# Redis lock prefix for the per-cluster primary decision. One triage agent at
# a time may act as the primary of a component/fixVersion cluster, so
# concurrently triaged issues cannot mark each other and form two clusters.
CLUSTER_LOCK_PREFIX = "lock:cluster:"


def rebase_cluster_lock_key(component: str, fix_version: str, downstream_component: str | None = None) -> str:
    """Redis lock key identifying a rebase sibling cluster.

    Mirrors the cluster identity used by ``build_siblings_jql``: same
    component and fix version — canonicalized across the Y-stream/Z-stream
    spellings the JQL matches as one cluster (``rhel-X.Y`` and ``rhel-X.Y.z``
    during GA transitions) — narrowed to the module stream for modular
    packages so e.g. postgresql:16 and postgresql:12 clusters lock
    independently.
    """
    # The same release spelled either way must lock the same key: the
    # discovery JQL treats both variants as one cluster.
    canonical_fix_version = sorted(get_fix_version_variants(fix_version))[0]
    if downstream_component and "/" in downstream_component:
        return f"{downstream_component}/{component}:{canonical_fix_version}"
    return f"{component}:{canonical_fix_version}"


def build_siblings_jql(
    issue_key: str,
    component: str,
    fix_version: str,
    excluded_labels: list[str],
    downstream_component: str | None = None,
) -> str:
    """
    Build JQL query to find sibling issues for consolidation.

    Args:
        issue_key: Primary issue key to exclude from results
        component: Package component name
        fix_version: Fix version to match (supports variants)
        excluded_labels: Jira labels to exclude (e.g., terminal triage labels)
        downstream_component: Raw Downstream Component Name (customfield_10669).
            For modular issues (``module:stream/package``), narrows results to
            the same module stream so e.g. postgresql:16 and postgresql:12
            siblings are not mixed.

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

    if downstream_component and "/" in downstream_component:
        escaped_dc = downstream_component.replace('"', '\\"')
        jql += f'AND cf[10669] = "{escaped_dc}" '

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
    exclude_triaged: bool = True,
    downstream_component: str | None = None,
) -> str:
    """
    Build JQL query to find rebase sibling candidates.

    Args:
        issue_key: Primary issue to exclude
        component: Package component
        fix_version: Target fix version
        exclude_triaged: If True, exclude all terminal states (for queueing new siblings).
                        If False, include all siblings (for consolidating in rebase MR).
        downstream_component: Raw Downstream Component Name for modular stream filtering.
    """
    excluded = []
    if exclude_triaged:
        # Exclude non-retriable terminal states to ensure JQL filtering before the 50-result limit.
        # Post-query filtering is not equivalent because we might miss real pending siblings
        # if there are >50 total candidates including many already-processed ones.
        #
        # Per jira_label_workflow_routing.md:
        # - ERRORED labels (triage/backport/rebase_errored) block retry → exclude (terminal)
        # - FAILED labels (backport/rebase_failed) may auto-retry → DO NOT exclude
        #
        # NOTE: Do NOT exclude ymir_rebase_sibling here - it's not a terminal triage state,
        # it's a queueing marker. Excluding it here would break check_and_queue_primary_if_ready()
        # which needs to find queued-but-not-started siblings to know if primary should wait.
        excluded = [
            # Triage decisions (non-retriable - sibling has been triaged and decided)
            JiraLabels.TRIAGED_NOT_AFFECTED.value,
            JiraLabels.TRIAGED_BACKPORT.value,
            JiraLabels.TRIAGED_REBUILD.value,
            JiraLabels.TRIAGED_REBASE.value,
            *JiraLabels.deferred_triage_labels(),
            JiraLabels.TRIAGED_POSTPONED.value,
            JiraLabels.TRIAGED.value,  # Open-ended-analysis, no automated follow-up
            # Completion labels (non-retriable - work successfully finished)
            JiraLabels.BACKPORTED.value,
            JiraLabels.REBASED.value,
            JiraLabels.REBUILT.value,
            # ERRORED labels (block retry, need human attention)
            JiraLabels.TRIAGE_ERRORED.value,
            JiraLabels.BACKPORT_ERRORED.value,
            JiraLabels.REBASE_ERRORED.value,
            JiraLabels.REBUILD_ERRORED.value,
            JiraLabels.NEEDS_ATTENTION.value,  # Clarification-needed, blocked
        ]
    return build_siblings_jql(
        issue_key=issue_key,
        component=component,
        fix_version=fix_version,
        excluded_labels=excluded,
        downstream_component=downstream_component,
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


async def _pending_rebase_siblings(
    primary_issue: str,
    component: str | None,
    fix_version: str | None,
    downstream_component: str | None,
    available_tools: list[Tool],
) -> list[str]:
    """Siblings of ``primary_issue`` that are still pending (queued or triaging).

    Raises when the search fails, mirroring the fail-closed behavior of the
    label reads: an undeterminable group must not be treated as finished.
    """
    if not component or not fix_version:
        raise RuntimeError(f"{primary_issue} missing component or fix_version")

    jql = build_rebase_siblings_jql(
        issue_key=primary_issue,
        component=component,
        fix_version=fix_version,
        downstream_component=downstream_component,
    )
    terminal_labels = [
        # Triage decisions
        JiraLabels.TRIAGED_REBASE.value,
        JiraLabels.TRIAGED_BACKPORT.value,
        JiraLabels.TRIAGED_REBUILD.value,
        *JiraLabels.deferred_triage_labels(),
        JiraLabels.TRIAGED_NOT_AFFECTED.value,
        JiraLabels.TRIAGED_POSTPONED.value,
        # Completions
        JiraLabels.BACKPORTED.value,
        JiraLabels.REBASED.value,
        JiraLabels.REBUILT.value,
        # Errors (won't proceed even if retriable)
        JiraLabels.TRIAGE_ERRORED.value,
        JiraLabels.BACKPORT_ERRORED.value,
        JiraLabels.REBASE_ERRORED.value,
        JiraLabels.REBUILD_ERRORED.value,
        # Failures (won't proceed)
        JiraLabels.BACKPORT_FAILED.value,
        JiraLabels.REBASE_FAILED.value,
        JiraLabels.REBUILD_FAILED.value,
    ]
    excluded = ", ".join(f'"{label}"' for label in terminal_labels)
    # Find siblings that are pending (have sibling/in-progress label) AND not terminal
    jql_pending = (
        f'{jql} AND (labels = "{JiraLabels.REBASE_SIBLING.value}" '
        f'OR labels = "{JiraLabels.TRIAGE_IN_PROGRESS.value}") '
        f"AND labels not in ({excluded})"
    )

    pending_siblings = await run_tool(
        "search_jira_issues",
        available_tools=available_tools,
        jql=jql_pending,
        fields=["key", "labels", "comment"],
        max_results=50,
    )

    # Filter to only siblings of THIS primary (verify via comment)
    actual_siblings = []
    for candidate in pending_siblings:
        candidate_key = candidate.get("key", "")
        # If it has ymir_rebase_sibling, it's definitely a sibling (not started yet)
        candidate_labels = candidate.get("fields", {}).get("labels", [])
        if JiraLabels.REBASE_SIBLING.value in candidate_labels:
            actual_siblings.append(candidate_key)
            continue
        # If it has ymir_triage_in_progress, check if it's a sibling of THIS primary
        if JiraLabels.TRIAGE_IN_PROGRESS.value in candidate_labels and is_marked_sibling_of(
            candidate, primary_issue
        ):
            actual_siblings.append(candidate_key)
    return actual_siblings


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

        # Look for a sibling marker comment naming this primary (SIBLING_MARKER_PREFIX)
        primary_issue = get_rebase_primary_issue(sibling_details)

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
        labels = primary_details.get("fields", {}).get("labels", [])
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
        downstream_component = fields.get("customfield_10669")

        # A sibling is "pending" (blocks the primary) if it has NOT finished processing:
        # ymir_rebase_sibling (queued but not started) or ymir_triage_in_progress,
        # without any terminal triage/completion/error label.
        actual_siblings = await _pending_rebase_siblings(
            primary_issue=primary_issue,
            component=component,
            fix_version=fix_version,
            downstream_component=downstream_component,
            available_tools=available_tools,
        )

        if actual_siblings:
            logger.info(
                f"Primary {primary_issue} still has {len(actual_siblings)} pending sibling(s): "
                f"{', '.join(actual_siblings)}, not ready to queue"
            )
            return

        # All siblings are done! Remove waiting label and queue primary for rebase
        action = (
            "[DRY-RUN] would remove waiting label and queue"
            if dry_run
            else "removing waiting label and queueing"
        )
        logger.info(f"All siblings done for {primary_issue}, {action} for rebase")

        if not dry_run:
            # Temporal consumes the waiting marker after receiving the request.
            await submit_triage_intent(
                await connect_triage_ingress(),
                TriageIntent(
                    primary_issue,
                    f"rebase-primary:{sibling_issue}:{fields.get('updated', 'initial')}",
                    "user_retry" if user_triggered else "retry",
                    dry_run=False,
                    triage_only=JiraLabels.TRIAGE_ONLY.value in labels,
                    retry_label=JiraLabels.WAITING_FOR_SIBLINGS.value,
                ),
            )
            logger.info(f"Re-queued {primary_issue} to triage (will re-triage and queue to rebase)")

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
    downstream_component: str | None = None,
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

    # Validate that the primary issue is still open/valid before consolidating siblings
    primary_details = None
    try:
        primary_details = await run_tool(
            "get_jira_details",
            available_tools=available_tools,
            issue_key=jira_issue,
        )
        primary_status = primary_details.get("fields", {}).get("status", {}).get("name", "")
        if primary_status in ("Closed", "Done", "Resolved"):
            logger.warning(
                f"Primary issue {jira_issue} is {primary_status}, "
                "skipping sibling consolidation (would create MR with invalid primary reference)"
            )
            return [], ""
    except Exception as e:
        logger.warning(f"Failed to check primary issue status for {jira_issue}: {e}, proceeding anyway")

    # If caller didn't supply downstream_component, read it from primary details
    if downstream_component is None and primary_details:
        downstream_component = primary_details.get("fields", {}).get("customfield_10669")

    try:
        # Build JQL to find siblings with ymir_triaged_rebase
        jql = build_siblings_jql(
            issue_key=jira_issue,
            component=rebase_data.package,
            fix_version=rebase_data.fix_version,
            excluded_labels=[],  # Don't exclude any labels
            downstream_component=downstream_component,
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
                # Note: get_jira_details tool should include comments by default
                candidate_details = await run_tool(
                    "get_jira_details",
                    available_tools=available_tools,
                    issue_key=candidate_key,
                )

                has_primary_reference = is_marked_sibling_of(candidate_details, jira_issue)
                if has_primary_reference:
                    logger.info(f"Sibling {candidate_key} confirmed as sibling of {jira_issue}")
                    consolidated_issue = ConsolidatedIssue(
                        issue_key=candidate_key,
                        cve_id=extract_cve_ids(candidate_details.get("fields", {}).get("summary", "")),
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
