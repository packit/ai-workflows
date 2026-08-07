"""No-patch sweep strategy.

Pushes postponed issues back to the triage queue for fresh evaluation by
the triage agent, subject to a ``MAX_ISSUES_PER_RUN`` cap.  The sweep
itself does not check a blocking condition — the triage agent decides
whether a patch is now available.

Handles issues tagged ``ymir_postponed_no_patch``.

Guardrails:
  - ``MAX_ISSUES_PER_RUN`` env var (default: 20) caps how many issues are
    pushed per run.  The remainder are checked on the next sweep.
  - Issues already being triaged (``ymir_triage_in_progress`` label) are
    skipped to avoid duplicates.
"""

import os

import redis

from ymir.common.base_utils import fix_await
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import Task
from ymir.supervisor.jira_utils import add_issue_label, remove_issue_label
from ymir.supervisor.supervisor_types import FullIssue
from ymir.sweep.base import SweepResult, SweepStrategy
from ymir.sweep.comment_parser import CommentData

_MAX_ISSUES_DEFAULT = 20


class NoPatchSweep(SweepStrategy):
    """Pushes no-patch issues back to the triage queue for re-evaluation.

    Overrides ``run()`` because the base class's comment-parsing loop
    does not fit this strategy: there is no blocking condition to check
    per issue; instead the strategy unconditionally re-queues eligible
    issues up to the configured cap.
    """

    name = "no_patch"
    label = JiraLabels.YMIR_POSTPONED_NO_PATCH

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        # Not used — run() is overridden.
        raise NotImplementedError("NoPatchSweep overrides run() directly")

    async def run(self, redis_conn: redis.Redis) -> dict[str, int]:
        """Re-queue eligible no-patch issues for fresh triage.

        Issues already in triage (``ymir_triage_in_progress`` label) are
        skipped.  At most ``MAX_ISSUES_PER_RUN`` issues are pushed in a
        single run; the rest wait for the next scheduled sweep.

        ``redis.RedisError`` is re-raised to abort the run (the CronJob's
        ``backoffLimit`` handles retries).

        Returns:
            A summary dict with counts compatible with the base class's
            ``run()`` return format.
        """
        max_issues = int(os.environ.get("MAX_ISSUES_PER_RUN", _MAX_ISSUES_DEFAULT))
        all_issues = self.get_blocked_issues()

        in_progress_count = 0
        eligible: list[FullIssue] = []
        for issue in all_issues:
            if JiraLabels.TRIAGE_IN_PROGRESS.value in issue.labels:
                in_progress_count += 1
            else:
                eligible.append(issue)

        if in_progress_count:
            self.logger.info("Skipping %d issues already in triage", in_progress_count)

        to_process = eligible[:max_issues]
        capped = len(eligible) - len(to_process)
        unblocked = 0
        errors = 0

        for issue in to_process:
            issue_key = issue.key
            try:
                task = Task.from_issue(issue_key)
                await fix_await(redis_conn.lpush(RedisQueues.TRIAGE_QUEUE.value, task.to_json()))
                add_issue_label(issue_key, JiraLabels.TRIAGE_IN_PROGRESS.value)
                remove_issue_label(issue_key, self.label.value)
                self.logger.info("Re-queued %s for fresh triage", issue_key)
                unblocked += 1
            except redis.RedisError:
                raise
            except Exception:
                self.logger.exception("Error re-queuing %s", issue_key)
                errors += 1

        still_blocked = in_progress_count + capped
        self.logger.info(
            "%s sweep complete: %d total, %d re-queued, %d capped (next run), "
            "%d already in triage, %d errors",
            self.name,
            len(all_issues),
            unblocked,
            capped,
            in_progress_count,
            errors,
        )
        return {
            "total": len(all_issues),
            "unblocked": unblocked,
            "transitioned": 0,
            "errors": errors,
            "still_blocked": still_blocked,
        }
