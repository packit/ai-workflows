"""SweepStrategy abstract base class for postponed-issue sweeps.

Provides the shared orchestration loop (fetch → parse comment → check →
unblock/transition) so that concrete strategies only need to implement
``is_unblocked()``. All Jira operations go through the shared
``ymir.common.jira_api`` async client. The MCP gateway is not involved.

The sweep runs inside ``asyncio.run()`` so that ``check_build_in_buildroot``
and provider requests can be awaited directly.
"""

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from ymir.common.base_utils import fix_await
from ymir.common.constants import JiraLabels
from ymir.common.jira_api import add_issue_label, get_current_issues, remove_issue_label
from ymir.common.models import FullIssue
from ymir.common.temporal_triage import TriageIntent, submit_triage_intent
from ymir.sweep.comment_parser import CommentData, parse_ymir_comment


@dataclass
class SweepResult:
    """Per-issue result from a sweep check."""

    issue_key: str
    action: Literal["unblocked", "transitioned", "still_blocked", "error"]
    detail: str


class SweepStrategy(ABC):
    """Base class for postponed-issue sweep strategies.

    Each subclass represents one postponement category.  The base class
    owns issue fetching, comment parsing, error handling, label management,
    and logging.  Subclasses implement only ``is_unblocked()``.

    Class attributes set by each subclass:

    ``name``
        Short identifier used in logging and CLI (e.g. ``"dependency"``).
    ``label``
        The ``JiraLabels`` member that tags issues belonging to this
        category (e.g. ``JiraLabels.YMIR_POSTPONED_DEPENDENCY``).
    """

    name: str
    label: JiraLabels

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"ymir.sweep.{self.name}")

    def get_blocked_issues(self) -> list[FullIssue]:
        """Query Jira for all issues with this strategy's postponement label.

        Returns ``FullIssue`` objects with comments, labels, and custom
        fields decoded by the shared Jira client.

        This synchronous compatibility entry point is retained for existing
        callers. The sweep runtime uses ``_get_blocked_issues_async``.
        """
        jql = f'labels = "{self.label.value}" AND status != "Closed"'
        result = get_current_issues(jql, full=True)
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                issues = list(asyncio.run(result))
            else:
                raise RuntimeError("Use _get_blocked_issues_async inside an event loop")
        else:
            issues = list(result)
        self.logger.info("Found %d issues with label %s", len(issues), self.label.value)
        return issues

    async def _get_blocked_issues_async(self) -> list[FullIssue]:
        jql = f'labels = "{self.label.value}" AND status != "Closed"'
        issues = list(await fix_await(get_current_issues(jql, full=True)))
        self.logger.info("Found %d issues with label %s", len(issues), self.label.value)
        return issues

    @abstractmethod
    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        """Check whether a single issue's blocking condition is resolved.

        Concrete strategies implement this method.  When the blocking
        condition is resolved, return ``SweepResult(action="unblocked",
        ...)``.  The ``detail`` field is posted as a Jira comment by
        ``on_unblock()``.

        If a state *transition* is appropriate (e.g. a closed MR means
        the issue should move from ``pr_pending`` to ``no_patch``), the
        strategy calls ``self.on_transition()`` directly and returns
        ``SweepResult(action="transitioned", ...)``.

        Args:
            issue: Decoded Jira ``FullIssue``.
            comment_data: Parsed blocker reference from the latest Ymir
                comment.
        """

    async def on_unblock(self, issue: FullIssue, temporal_client, comment: str | None = None) -> None:
        """Submit the retry and let the workflow consume the postponement label."""
        revision = ":".join(sorted(item.id for item in issue.comments))
        event = sha256(f"{self.label.value}:{revision}".encode()).hexdigest()
        await submit_triage_intent(
            temporal_client,
            TriageIntent(
                issue.key,
                f"sweep:{event}",
                "retry",
                dry_run=False,
                triage_only=JiraLabels.TRIAGE_ONLY.value in issue.labels,
                retry_label=self.label.value,
                retry_comment=comment,
            ),
        )
        self.logger.info("Submitted %s for Temporal re-triage", issue.key)

    def on_transition(
        self,
        issue_key: str,
        new_label: JiraLabels,
        comment: str | None = None,
    ):
        """Swap postponement label and post an optional comment.

        Adds the new label first so that a subsequent failure to remove the
        old label leaves the issue discoverable by the new strategy.  The
        comment is posted atomically with the new label addition.
        """

        async def transition() -> None:
            await fix_await(add_issue_label(issue_key, new_label.value, comment=comment))
            await fix_await(remove_issue_label(issue_key, self.label.value))
            self.logger.info(
                "Transitioned %s: %s -> %s",
                issue_key,
                self.label.value,
                new_label.value,
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(transition())
            return None
        return transition()

    async def run(self, temporal_client) -> dict[str, int]:
        """Execute a full sweep: fetch issues, check each, handle results.

        This is the shared orchestration logic.  It handles comment
        parsing, error handling per issue, and summary logging.  Subclasses
        do not override this — they only implement ``is_unblocked()``.

        Request failures leave the issue's postponement label in place so
        the next sweep can retry the same request.

        Returns:
            A summary dict with counts: ``total``, ``unblocked``,
            ``transitioned``, ``errors``, ``still_blocked``.
        """
        issues = await self._get_blocked_issues_async()
        total = len(issues)
        unblocked = 0
        transitioned = 0
        errors = 0

        for issue in issues:
            issue_key = issue.key
            try:
                comment_data = parse_ymir_comment(issue)
                if not comment_data:
                    self.logger.warning("No parseable Ymir comment on %s, skipping", issue_key)
                    errors += 1
                    continue

                result = await self.is_unblocked(issue, comment_data)

                if result.action == "unblocked":
                    await self.on_unblock(issue, temporal_client, comment=result.detail)
                    unblocked += 1
                elif result.action == "transitioned":
                    transitioned += 1
                elif result.action == "error":
                    self.logger.warning("Check error on %s: %s", issue_key, result.detail)
                    errors += 1
            except Exception:
                self.logger.exception("Unhandled error checking %s", issue_key)
                errors += 1

        still_blocked = total - unblocked - transitioned - errors
        self.logger.info(
            "%s sweep complete: %d total, %d unblocked, %d transitioned, %d errors, %d still blocked",
            self.name,
            total,
            unblocked,
            transitioned,
            errors,
            still_blocked,
        )
        return {
            "total": total,
            "unblocked": unblocked,
            "transitioned": transitioned,
            "errors": errors,
            "still_blocked": still_blocked,
        }
