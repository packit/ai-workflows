"""PR-pending sweep strategy.

Checks whether the identified upstream pull/merge request has been merged.
Supports both GitLab MRs (via the GitLab REST API) and GitHub PRs (via the
GitHub REST API).

GitLab: uses ``gitlab_utils.gitlab_api_get()`` with the ``gitlab_url``
parameter to support both ``gitlab.com`` and internal hosts such as
``gitlab.cee.redhat.com``.

GitHub: uses ``github_utils.github_api_get()``.  Authentication is
optional (set ``GITHUB_TOKEN`` ); all upstream
projects tracked by Ymir are public, so unauthenticated access suffices.

Handles issues tagged ``ymir_postponed_pr_pending``.

State transitions:
  - PR/MR ``merged``  → unblock (remove label, push to triage queue)
  - PR/MR ``closed``  → transition to ``ymir_postponed_no_patch`` (the
                         fix is no longer coming via this PR/MR)
  - PR/MR ``opened``  → still blocked, no action
"""

import re
from urllib.parse import quote as urlquote

from ymir.common.constants import JiraLabels
from ymir.supervisor.github_utils import github_api_get
from ymir.supervisor.gitlab_utils import ALLOWED_GITLAB_HOSTS, gitlab_api_get
from ymir.supervisor.supervisor_types import FullIssue, MergeRequestState
from ymir.sweep.base import SweepResult, SweepStrategy
from ymir.sweep.comment_parser import CommentData

# Only match MR URLs on trusted GitLab hosts.  ``blocker_references`` values are
# LLM-authored and untrusted, and the extracted host is used to build an
# authenticated API call; restricting the host here (in addition to the guard
# in ``gitlab_api_get``) yields a clean "not recognisable" error rather than an
# exception for hostile URLs.  Longest host first so alternation is unambiguous.
_GITLAB_HOSTS_ALT = "|".join(re.escape(h) for h in sorted(ALLOWED_GITLAB_HOSTS, key=len, reverse=True))
_GITLAB_MR_RE = re.compile(rf"https://({_GITLAB_HOSTS_ALT})/(.+?)/-/merge_requests/(\d+)")
_GITHUB_PR_RE = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _fetch_gitlab_mr_state(mr_url: str, m: re.Match) -> tuple[str, str | None]:
    """Return ``(state, error_detail)`` for a GitLab MR. error_detail is None on success."""
    host, project_path, mr_iid = m.group(1), m.group(2), m.group(3)
    path = f"projects/{urlquote(project_path, safe='')}/merge_requests/{mr_iid}"
    try:
        data = gitlab_api_get(path, gitlab_url=f"https://{host}")
    except Exception as exc:
        return "", f"GitLab API call failed for {mr_url}: {exc}"
    return data.get("state", ""), None


def _fetch_github_pr_state(pr_url: str, m: re.Match) -> tuple[str, str | None]:
    """Return ``(state, error_detail)`` for a GitHub PR. error_detail is None on success.

    GitHub PRs use ``state: open|closed`` plus ``merged_at`` to distinguish a
    merged close from an abandoned close.  The returned state is normalised to
    the same values ``MergeRequestState`` uses (``opened``, ``merged``,
    ``closed``) so downstream logic is identical for both platforms.
    """
    owner_repo, pr_number = m.group(1), m.group(2)
    try:
        data = github_api_get(f"repos/{owner_repo}/pulls/{pr_number}")
    except Exception as exc:
        return "", f"GitHub API call failed for {pr_url}: {exc}"

    gh_state = data.get("state", "")
    if gh_state == "open":
        return MergeRequestState.OPEN, None
    if gh_state == "closed":
        if data.get("merged_at"):
            return MergeRequestState.MERGED, None
        return MergeRequestState.CLOSED, None
    # Unknown state — pass through; the caller maps it to still_blocked.
    return gh_state, None


class PRPendingSweep(SweepStrategy):
    """Checks whether the upstream pull/merge request has been merged.

    For each issue the strategy:

    1. Reads the PR/MR URL from the comment's ``*Blockers*:`` line.
    2. Detects the platform from the URL (GitLab or GitHub).
    3. Fetches the PR/MR state via the appropriate REST API.
    4. Acts on the state:
       - ``merged`` → unblock
       - ``closed`` → transition to ``ymir_postponed_no_patch``
       - ``opened`` → still blocked
    """

    name = "pr_pending"
    label = JiraLabels.YMIR_POSTPONED_PR_PENDING

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        issue_key = issue.key

        pr_url = comment_data.blocker_references[0] if comment_data.blocker_references else None
        if not pr_url:
            return SweepResult(
                issue_key=issue_key,
                action="error",
                detail=f"No *Blockers*: PR/MR URL found in comment for {issue_key}",
            )

        gl_match = _GITLAB_MR_RE.match(pr_url)
        gh_match = _GITHUB_PR_RE.match(pr_url)

        if gl_match:
            state, error = _fetch_gitlab_mr_state(pr_url, gl_match)
        elif gh_match:
            state, error = _fetch_github_pr_state(pr_url, gh_match)
        else:
            return SweepResult(
                issue_key=issue_key,
                action="error",
                detail=(
                    f"Blocker reference is not a recognisable GitLab MR or "
                    f"GitHub PR URL for {issue_key}: {pr_url!r}"
                ),
            )

        if error:
            return SweepResult(issue_key=issue_key, action="error", detail=error)

        if state == MergeRequestState.MERGED:
            return SweepResult(
                issue_key=issue_key,
                action="unblocked",
                detail=f"Upstream PR/MR {pr_url} has been merged. Re-triaging.",
            )

        if state == MergeRequestState.CLOSED:
            # PR/MR was abandoned — no fix is coming via this URL any more.
            # Transition to no_patch so the no-patch sweep re-evaluates.
            self.on_transition(
                issue_key,
                JiraLabels.YMIR_POSTPONED_NO_PATCH,
                comment=(
                    f"Upstream PR/MR {pr_url} was closed without merging. "
                    "Transitioning to no-patch state for re-evaluation."
                ),
            )
            return SweepResult(
                issue_key=issue_key,
                action="transitioned",
                detail=f"PR/MR {pr_url} closed — transitioned to no_patch",
            )

        # PR/MR is still open (or in an unexpected state).
        return SweepResult(
            issue_key=issue_key,
            action="still_blocked",
            detail=f"PR/MR {pr_url} is still {state!r}",
        )
