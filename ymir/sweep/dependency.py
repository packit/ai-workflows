"""Dependency sweep strategy.

Checks whether the dependency's fixed build is present in the Y-stream
buildroot.  Uses ``jira_utils.get_issue()`` for blocker lookup and
``check_build_in_buildroot()`` for buildroot verification.

Handles issues tagged ``ymir_postponed_dependency``, which are set when
the rebuild triage agent finds that a dependency's fixed build has not
yet landed in the target buildroot.
"""

import re

import requests

from ymir.common.config import load_rhel_config
from ymir.common.constants import JiraLabels
from ymir.common.utils import check_build_in_buildroot
from ymir.common.version_utils import normalize_fix_version, parse_rhel_version
from ymir.supervisor.jira_utils import get_issue
from ymir.supervisor.supervisor_types import FullIssue
from ymir.sweep.base import SweepResult, SweepStrategy
from ymir.sweep.comment_parser import CommentData

JIRA_KEY_RE = re.compile(r"^[A-Z]+-\d+$")
# Matches "to land in c9s buildroot" or "to land in rhel-9.8.0 buildroot"
_BRANCH_FROM_SUMMARY_RE = re.compile(r"to land in (\S+) buildroot")


def branch_from_fix_versions(fix_versions: list[str]) -> str | None:
    """Derive a CentOS Stream dist-git branch from an issue's fix_versions.

    Parses the first recognisable RHEL version string (e.g. ``rhel-9.6.0``)
    and maps it to the corresponding CS branch (e.g. ``c9s``).
    """
    for fv in fix_versions:
        parsed = parse_rhel_version(fv)
        if parsed:
            major, _, _ = parsed
            return f"c{major}s"
    return None


def _resolve_target_branch(comment_data: CommentData, fix_versions: list[str]) -> str | None:
    """Return the target branch for the buildroot check.

    Tries to extract the branch from the ``*Summary*:`` line of the Ymir
    comment (format: ``"...to land in <branch> buildroot"``), then falls
    back to deriving it from the issue's fix_versions.
    """
    if comment_data.summary:
        m = _BRANCH_FROM_SUMMARY_RE.search(comment_data.summary)
        if m:
            return m.group(1)
    return branch_from_fix_versions(fix_versions)


class DependencySweep(SweepStrategy):
    """Checks whether the dependency's fixed build is present in the buildroot.

    For each issue the strategy:

    1. Reads the blocker Jira issue key from the comment's ``*Blockers*:``
       line (or the first ``*Waiting for*:`` entry).
    2. Fetches the blocker issue via ``jira_utils.get_issue()`` and reads
       its ``fixed_in_build`` field.
    3. If ``fixed_in_build`` is absent → still blocked.
    4. Derives ``target_branch`` from the comment summary or issue's
       ``fix_versions`` field.
    5. Reads ``dep_component`` from the blocker's ``components`` list.
    6. Calls ``check_build_in_buildroot()`` to verify availability.
    7. If the build is present → unblocks the issue.
    """

    name = "dependency"
    label = JiraLabels.YMIR_POSTPONED_DEPENDENCY

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:

        # Resolve blocker key from comment (prefer explicit Blockers line;
        # fall back to first pending issue entry).
        blocker_key = comment_data.blocker_references[0] if comment_data.blocker_references else None
        if not blocker_key and comment_data.pending_issues:
            blocker_key = comment_data.pending_issues[0]

        if not blocker_key or not JIRA_KEY_RE.match(blocker_key):
            return SweepResult(
                issue_key=issue.key,
                action="error",
                detail=f"No valid Jira blocker key found in comment for {issue.key}",
            )

        try:
            blocker = get_issue(blocker_key)
        except requests.HTTPError as exc:
            return SweepResult(
                issue_key=issue.key,
                action="error",
                detail=f"Failed to fetch blocker {blocker_key}: {exc}",
            )

        if not blocker.fixed_in_build:
            return SweepResult(
                issue_key=issue.key,
                action="still_blocked",
                detail=f"{blocker_key} has no Fixed in Build yet",
            )

        if not blocker.components:
            return SweepResult(
                issue_key=issue.key,
                action="error",
                detail=f"Blocker {blocker_key} has no components — cannot determine dep_component",
            )
        dep_component = blocker.components[0]

        target_branch = _resolve_target_branch(comment_data, issue.fix_versions)
        if not target_branch:
            return SweepResult(
                issue_key=issue.key,
                action="error",
                detail=f"Cannot determine target branch for {issue.key} (fix_versions={issue.fix_versions})",
            )

        raw_fix_version = issue.fix_versions[0] if issue.fix_versions else ""
        if raw_fix_version:
            rhel_config = await load_rhel_config()
            fix_version = normalize_fix_version(raw_fix_version, rhel_config)
        else:
            fix_version = raw_fix_version

        try:
            in_buildroot = await check_build_in_buildroot(
                target_branch,
                dep_component,
                blocker.fixed_in_build,
                fix_version=fix_version,
            )
        except Exception as exc:
            return SweepResult(
                issue_key=issue.key,
                action="error",
                detail=(f"Buildroot check failed for {dep_component} ({blocker.fixed_in_build}): {exc}"),
            )

        if in_buildroot:
            return SweepResult(
                issue_key=issue.key,
                action="unblocked",
                detail=(
                    f"Dependency {blocker_key} now has Fixed in Build "
                    f"({blocker.fixed_in_build}), confirmed present in "
                    f"{target_branch} buildroot. Re-triaging."
                ),
            )

        return SweepResult(
            issue_key=issue.key,
            action="still_blocked",
            detail=(f"{blocker.fixed_in_build} not yet in {target_branch} buildroot"),
        )
