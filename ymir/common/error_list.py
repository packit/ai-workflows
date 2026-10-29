"""Remove resolved workflow failures from the active error list."""

import json
import logging
from collections.abc import Collection

from ymir.common.base_utils import fix_await
from ymir.common.constants import RedisQueues

logger = logging.getLogger(__name__)

# Failures are LPUSHed, so the prefix contains the most recent failures. Bound
# retrieval and JSON parsing even as historical error data accumulates.
ERROR_CLEANUP_SCAN_LIMIT = 1000


async def clear_resolved_errors(
    redis_conn,
    jira_issue: str | Collection[str],
    queue: str,
    *,
    target_branch: str | None = None,
    dry_run: bool = False,
) -> int:
    """Clear recent failures for one or more issues, workflow, and target branch.

    The priority and ordinary queues represent the same workflow. Match the
    branch as well because one issue can have independent work on several RHEL
    streams. Remove the original bytes with LREM so concurrent list changes are
    preserved. Legacy entries without a queue or task cannot be matched safely.
    A dry run does not read or modify the production error list.
    Batch consolidated siblings into one scan of the newest 1,000 entries.
    Older entries remain available for manual inspection and requeueing.
    """
    if dry_run:
        return 0

    resolved_issues = {jira_issue} if isinstance(jira_issue, str) else set(jira_issue)
    if not resolved_issues:
        return 0

    resolved_queue = queue.removesuffix("_todo")
    removed = 0
    try:
        entries = await fix_await(
            redis_conn.lrange(RedisQueues.ERROR_LIST.value, 0, ERROR_CLEANUP_SCAN_LIMIT - 1)
        )
        for raw in entries:
            try:
                entry = json.loads(raw)
                task = entry.get("task")
                error = entry.get("error")
                if not isinstance(task, dict) or not isinstance(error, dict):
                    continue
                if entry.get("queue", "").removesuffix("_todo") != resolved_queue:
                    continue
                if error.get("jira_issue") not in resolved_issues:
                    continue
                if task.get("metadata", {}).get("target_branch") != target_branch:
                    continue
            except (TypeError, ValueError, AttributeError):
                continue
            removed += await fix_await(redis_conn.lrem(RedisQueues.ERROR_LIST.value, 1, raw))
    except Exception:
        # The workflow has already succeeded. A cleanup failure must not turn
        # that success into a retry or create another error-list entry.
        logger.exception("Failed to clear resolved errors for %s from %s", resolved_issues, resolved_queue)
    return removed
