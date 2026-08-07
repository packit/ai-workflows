"""SweepStrategy abstract base class for postponed-issue sweeps.

Provides the shared orchestration loop (fetch → parse comment → check →
unblock/transition) so that concrete strategies only need to implement
``is_unblocked()``.  All Jira operations go through
``ymir.supervisor.jira_utils`` (synchronous ``requests``-based client).
The MCP gateway is not involved.

The sweep runs inside ``asyncio.run()`` so that ``check_build_in_buildroot``
can be awaited directly.  ``jira_utils`` calls remain synchronous; they
block the event loop briefly, which is acceptable for a batch CronJob.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import redis

from ymir.common.base_utils import fix_await
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import Task
from ymir.supervisor.jira_utils import (
    add_issue_label,
    get_current_issues,
    remove_issue_label,
)
from ymir.supervisor.supervisor_types import FullIssue
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
        fields decoded via ``jira_utils.get_current_issues(jql, full=True)``.
        """
        jql = f'labels = "{self.label.value}"'
        issues = list(get_current_issues(jql, full=True))
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

    async def on_unblock(
        self,
        issue_key: str,
        redis_conn: redis.Redis,
        comment: str | None = None,
    ) -> None:
        """Push issue to triage queue, then remove the postponement label.

        Redis is written first so that a subsequent Jira API failure leaves
        the issue labelled (retried on the next sweep) rather than
        unlabelled but missing from the queue.  The comment is posted
        atomically with the label removal when provided.
        """
        task = Task.from_issue(issue_key)
        await fix_await(redis_conn.lpush(RedisQueues.TRIAGE_QUEUE.value, task.to_json()))
        add_issue_label(issue_key, JiraLabels.TRIAGE_IN_PROGRESS.value)
        remove_issue_label(issue_key, self.label.value, comment=comment)
        self.logger.info(
            "Unblocked %s — removed %s, pushed to triage queue",
            issue_key,
            self.label.value,
        )

    def on_transition(
        self,
        issue_key: str,
        new_label: JiraLabels,
        comment: str | None = None,
    ) -> None:
        """Swap postponement label and post an optional comment.

        Adds the new label first so that a subsequent failure to remove the
        old label leaves the issue discoverable by the new strategy.  The
        comment is posted atomically with the new label addition.
        """
        add_issue_label(issue_key, new_label.value, comment=comment)
        remove_issue_label(issue_key, self.label.value)
        self.logger.info(
            "Transitioned %s: %s -> %s",
            issue_key,
            self.label.value,
            new_label.value,
        )

    async def run(self, redis_conn: redis.Redis) -> dict[str, int]:
        """Execute a full sweep: fetch issues, check each, handle results.

        This is the shared orchestration logic.  It handles comment
        parsing, error handling per issue, and summary logging.  Subclasses
        do not override this — they only implement ``is_unblocked()``.

        ``redis.RedisError`` and ``OSError`` exceptions are re-raised to abort
        the sweep (the CronJob's ``backoffLimit`` handles retries).
        ``OSError`` surfaces configuration errors such as missing environment
        variables.  All other per-issue exceptions are caught, logged, and
        counted as errors.

        Returns:
            A summary dict with counts: ``total``, ``unblocked``,
            ``transitioned``, ``errors``, ``still_blocked``.
        """
        issues = self.get_blocked_issues()
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
                    await self.on_unblock(issue_key, redis_conn, comment=result.detail)
                    unblocked += 1
                elif result.action == "transitioned":
                    transitioned += 1
                elif result.action == "error":
                    self.logger.warning("Check error on %s: %s", issue_key, result.detail)
                    errors += 1
            except (redis.RedisError, OSError):
                raise
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
