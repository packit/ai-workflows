import asyncio
import logging
import re
from pathlib import Path

from beeai_framework.tools import Tool
from specfile import Specfile

import ymir.agents.tasks as tasks
from ymir.agents.utils import (
    run_tool,
)
from ymir.common.constants import JiraLabels
from ymir.common.cve import extract_cve_ids
from ymir.common.models import ConsolidatedIssue, RebaseData
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
def build_siblings_jql(
    issue_key: str,
    component: str,
    fix_version: str,
    downstream_component: str | None = None,
) -> str:
    """
    Build JQL query to find sibling issues for consolidation.

    Args:
        issue_key: Primary issue key to exclude from results
        component: Package component name
        fix_version: Fix version to match (supports variants)
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

    jql += 'AND status in ("New", "Planning")'
    return jql


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
