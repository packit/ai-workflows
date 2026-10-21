"""Redis ZSET helpers for delaying agent-queue tasks until a ready time."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import redis.asyncio as redis

from ymir.common.base_utils import fix_await

logger = logging.getLogger(__name__)

# A stale ZRANGEBYSCORE result must not let two workers enqueue the same retry.
_PROMOTE_TASK = """
local ready_at = redis.call('ZSCORE', KEYS[1], ARGV[1])
if ready_at and tonumber(ready_at) <= tonumber(ARGV[2]) then
    redis.call('LPUSH', KEYS[2], ARGV[1])
    redis.call('ZREM', KEYS[1], ARGV[1])
    return 1
end
return 0
"""


async def schedule_task(
    redis_conn: redis.Redis,
    delayed_key: str,
    payload: str,
    delay_seconds: float,
) -> None:
    """Add ``payload`` to a delayed ZSET, ready after ``delay_seconds``."""
    ready_at = time.time() + max(0.0, delay_seconds)
    await fix_await(redis_conn.zadd(delayed_key, {payload: ready_at}))
    logger.info(
        "Scheduled task on %s for %.0fs from now (ready_at=%.0f)",
        delayed_key,
        delay_seconds,
        ready_at,
    )


async def promote_due_tasks(
    redis_conn: redis.Redis,
    delayed_key: str,
    target_queue_for_payload: Callable[[str], str],
    *,
    now: float | None = None,
) -> int:
    """Move due members from ``delayed_key`` onto their target list queues.

    Returns the number of tasks promoted.
    """
    cutoff = time.time() if now is None else now
    due = await fix_await(redis_conn.zrangebyscore(delayed_key, min="-inf", max=cutoff))
    if not due:
        return 0

    promoted = 0
    for member in due:
        payload = member.decode() if isinstance(member, bytes) else str(member)
        target_queue = target_queue_for_payload(payload)
        claimed = await fix_await(
            redis_conn.eval(_PROMOTE_TASK, 2, delayed_key, target_queue, member, cutoff)
        )
        if claimed:
            promoted += 1
            logger.info("Promoted delayed task from %s to %s", delayed_key, target_queue)

    return promoted
