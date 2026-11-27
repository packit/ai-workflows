from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from ymir.common.base_utils import fix_await

logger = logging.getLogger(__name__)

LOCK_KEY_PREFIX = "lock:triage:"
BACKPORT_LOCK_KEY_PREFIX = "lock:backport:"
REBASE_LOCK_KEY_PREFIX = "lock:rebase:"
REBUILD_LOCK_KEY_PREFIX = "lock:rebuild:"
# Issue-scoped locks used to prevent duplicate queue scheduling. Keep cluster
# coordination locks out of this collection: they do not identify an issue.
WORKFLOW_LOCK_PREFIXES = (
    LOCK_KEY_PREFIX,
    BACKPORT_LOCK_KEY_PREFIX,
    REBASE_LOCK_KEY_PREFIX,
    REBUILD_LOCK_KEY_PREFIX,
)
DEFAULT_LOCK_TTL_MS = 300_000  # 5 minutes


# Compare-and-delete for string keys.  Analogous to _CONDITIONAL_HDEL_LUA in
# merge_queue.py which does the same for hash fields.
_CONDITIONAL_DEL_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
end
return 0
"""

# Compare-and-extend: only refresh TTL if we still own the lock.
_CONDITIONAL_PEXPIRE_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
end
return 0
"""


def _lock_key(issue_key: str, prefix: str = LOCK_KEY_PREFIX) -> str:
    """Build a Redis lock key from the uppercased Jira issue key."""
    return f"{prefix}{issue_key.upper()}"


async def acquire_issue_lock(
    redis_conn: aioredis.Redis,
    issue_key: str,
    token: str,
    ttl_ms: int = DEFAULT_LOCK_TTL_MS,
    prefix: str = LOCK_KEY_PREFIX,
) -> bool:
    """Acquire a per-issue lock.

    Uses SET NX PX for atomic acquire-with-expiry.

    Returns True if acquired, False if already held by another holder.
    """
    result = await fix_await(redis_conn.set(_lock_key(issue_key, prefix), token, nx=True, px=ttl_ms))
    return result is True or result == b"OK"


async def release_issue_lock(
    redis_conn: aioredis.Redis,
    issue_key: str,
    token: str,
    prefix: str = LOCK_KEY_PREFIX,
) -> bool:
    """Release a per-issue lock, but only if still owned by this token.

    Uses a compare-and-delete Lua script for atomicity.

    Returns True if released, False if already expired or owned by another.
    """
    result = await fix_await(redis_conn.eval(_CONDITIONAL_DEL_LUA, 1, _lock_key(issue_key, prefix), token))
    return result == 1


async def extend_issue_lock(
    redis_conn: aioredis.Redis,
    issue_key: str,
    token: str,
    ttl_ms: int = DEFAULT_LOCK_TTL_MS,
    prefix: str = LOCK_KEY_PREFIX,
) -> bool:
    """Extend the TTL of a lock, but only if still owned by this token.

    Uses a compare-and-PEXPIRE Lua script for atomicity.

    Returns True if extended, False if already expired or owned by another.
    """
    result = await fix_await(
        redis_conn.eval(_CONDITIONAL_PEXPIRE_LUA, 1, _lock_key(issue_key, prefix), token, ttl_ms)
    )
    return result == 1


async def _heartbeat_loop(
    redis_conn: aioredis.Redis,
    issue_key: str,
    token: str,
    ttl_ms: int,
    prefix: str = LOCK_KEY_PREFIX,
) -> None:
    """Refresh an issue lock every third of its TTL until cancelled or lost.

    Redis errors are logged and retried on the next interval.
    """
    interval = ttl_ms / 3000.0
    while True:
        await asyncio.sleep(interval)
        try:
            extended = await extend_issue_lock(redis_conn, issue_key, token, ttl_ms, prefix)
            if not extended:
                # Lock expired (Redis outage > TTL) and was re-acquired
                # by another worker.  We stop heartbeating but do NOT
                # cancel the task — see issue_lock() docstring.
                logger.error(
                    "Heartbeat failed: lock for %s no longer owned by us (token=%s)",
                    issue_key,
                    token[:8],
                )
                return
        except Exception:
            logger.warning(
                "Redis error during heartbeat for %s; will retry in %.1fs",
                issue_key,
                interval,
                exc_info=True,
            )


@asynccontextmanager
async def issue_lock(
    redis_conn: aioredis.Redis,
    issue_key: str,
    ttl_ms: int = DEFAULT_LOCK_TTL_MS,
    prefix: str = LOCK_KEY_PREFIX,
) -> AsyncGenerator[str | None, None]:
    """Acquire a per-issue lock, maintain it via heartbeat, release on exit.

    Yields the lock token if acquired, None if already held by another
    worker.  On exit (including CancelledError), cancels the heartbeat
    and releases the lock.

    If Redis becomes unreachable for longer than ``ttl_ms`` the lock
    auto-expires and another worker can acquire it.  The heartbeat
    detects this and logs an error but does NOT cancel the running task
    — the Jira in-progress label acts as a fallback dedup anchor.  This
    trades a small risk of duplicate work for avoiding the abort of a
    long-running task due to a transient Redis outage.
    """
    token = str(uuid.uuid4())
    acquired = await acquire_issue_lock(redis_conn, issue_key, token, ttl_ms, prefix)
    if not acquired:
        yield None
        return

    heartbeat_task = asyncio.create_task(_heartbeat_loop(redis_conn, issue_key, token, ttl_ms, prefix))
    try:
        yield token
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        try:
            await release_issue_lock(redis_conn, issue_key, token, prefix)
        except Exception:
            logger.warning(
                "Failed to release lock for %s during cleanup; lock will auto-expire in <=%.0fs",
                issue_key,
                ttl_ms / 1000,
                exc_info=True,
            )


DEFAULT_WAITING_LOCK_MAX_WAIT_SECONDS = 600
DEFAULT_WAITING_LOCK_POLL_SECONDS = 5.0


@asynccontextmanager
async def waiting_issue_lock(
    redis_conn: aioredis.Redis,
    lock_key: str,
    ttl_ms: int = DEFAULT_LOCK_TTL_MS,
    prefix: str = LOCK_KEY_PREFIX,
    max_wait_seconds: float = DEFAULT_WAITING_LOCK_MAX_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_WAITING_LOCK_POLL_SECONDS,
) -> AsyncGenerator[bool, None]:
    """Acquire a lock, waiting for a concurrent holder to release it.

    Unlike :func:`issue_lock`, which yields ``None`` immediately when busy,
    this variant polls until the lock becomes free or ``max_wait_seconds``
    elapses. Yields ``True`` when acquired, ``False`` otherwise (still busy
    past the deadline, or Redis unreachable) - callers are expected to
    proceed unlocked in that case, trading strictness for availability.

    While held, the lock is maintained via heartbeat and released on exit,
    exactly like :func:`issue_lock`.
    """
    token = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_seconds
    while True:
        try:
            acquired = await acquire_issue_lock(redis_conn, lock_key, token, ttl_ms, prefix)
        except Exception:
            logger.warning("Could not reach Redis for lock %s; proceeding unlocked", lock_key, exc_info=True)
            yield False
            return
        if acquired:
            break
        if loop.time() >= deadline:
            logger.warning("Lock %s still busy after %.0fs; proceeding unlocked", lock_key, max_wait_seconds)
            yield False
            return
        await asyncio.sleep(poll_seconds)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(redis_conn, lock_key, token, ttl_ms, prefix))
    try:
        yield True
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        try:
            await release_issue_lock(redis_conn, lock_key, token, prefix)
        except Exception:
            logger.warning(
                "Failed to release lock for %s during cleanup; lock will auto-expire in <=%.0fs",
                lock_key,
                ttl_ms / 1000,
                exc_info=True,
            )
