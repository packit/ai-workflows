"""Parse blocker references from Ymir triage comments on Jira issues.

Extracts structured postponement data (blocker reference, pending issues)
from the machine-readable fields that the triage agent writes into Jira
comments via ``format_for_comment()``.

The postponement *reason* is not read from the comment: sweeps select
issues by their ``ymir_postponed_*`` label (see ``SweepStrategy``), so the
label — not a comment field — is the authoritative category signal.
"""

import os
import re
from dataclasses import dataclass

from ymir.supervisor.supervisor_types import FullIssue

_YMIR_TRIAGE_AGENT_COMMENT_MARKER = "Output from Ymir Triage Agent"

_BLOCKER_RE = re.compile(r"^\*Blockers?\*:\s*(.+)$", re.MULTILINE)
_PENDING_ISSUE_RE = re.compile(r"^\* ([A-Z]+-\d+)$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^\*Summary\*:\s*(.+)$", re.MULTILINE)


@dataclass
class CommentData:
    """Structured data extracted from a Ymir triage comment."""

    blocker_references: list[str] | None
    pending_issues: list[str]
    summary: str | None
    comment_id: str


def parse_ymir_comment(issue: FullIssue) -> CommentData | None:
    """Extract blocker reference from the latest Ymir comment on an issue.

    Searches the issue's comments (``JiraComment`` objects with ``.body``,
    ``.id``, ``.authorName``, ``.created`` fields) in reverse chronological
    order for the latest one containing the Ymir triage marker.  Extracts
    structured fields via regex from the ``*Blocker*:``, ``*Waiting for*:``
    / ``*Waiting for at least one of*:``, and ``*Summary*:`` lines.

    Args:
        issue: Decoded Jira ``FullIssue`` from
            ``jira_utils.get_issue(key, full=True)``.

    Returns:
        ``CommentData`` with blocker_references, pending_issues, summary, and
        comment_id.  Returns ``None`` if no Ymir comment is found.  Fields
        that are absent from the comment are ``None``/empty; each sweep
        strategy validates the fields it needs.
    """
    jira_email = os.environ.get("JIRA_EMAIL")
    if not jira_email:
        raise OSError("JIRA_EMAIL environment variable is not set")

    ymir_comment = None
    for comment in reversed(issue.comments):
        if _YMIR_TRIAGE_AGENT_COMMENT_MARKER in comment.body and comment.authorEmail == jira_email:
            ymir_comment = comment
            break

    if ymir_comment is None:
        return None

    body = ymir_comment.body

    blocker_match = _BLOCKER_RE.search(body)
    blocker_references = [b.strip() for b in blocker_match.group(1).split(",")] if blocker_match else None

    pending_issues = _PENDING_ISSUE_RE.findall(body)

    summary_match = _SUMMARY_RE.search(body)
    summary = summary_match.group(1).strip() if summary_match else None

    return CommentData(
        blocker_references=blocker_references,
        pending_issues=pending_issues,
        summary=summary,
        comment_id=ymir_comment.id,
    )
