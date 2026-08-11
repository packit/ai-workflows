"""Redis exclusive lock for reproducer create/adapt on a shared tests path.

Sibling-stream workers (e.g. rhel-10 then rhel-9/rhel-8) serialize on
``package:lock_id`` so only one worker creates or adapts the canonical
``Security/<CVE>/`` or ``Regression/<JIRA>/`` test at a time.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from ymir.common.base_utils import fix_await

logger = logging.getLogger(__name__)

REPRODUCER_LOCK_HASH = "reproducer_creation_lock"
_DEFAULT_STALE_THRESHOLD = timedelta(hours=6)

_ACQUIRE_LUA = """
local hash = KEYS[1]
local field = ARGV[1]
local value = ARGV[2]
if redis.call('HEXISTS', hash, field) == 1 then
    return 0
end
redis.call('HSET', hash, field, value)
return 1
"""

_CONDITIONAL_HDEL_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    redis.call('HDEL', KEYS[1], ARGV[1])
    return 1
end
return 0
"""


class ReproducerLockEntry(BaseModel):
    """Stored value for an active reproducer create/adapt lock."""

    package: str
    lock_id: str
    jira_issue: str | None = None
    activated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def reproducer_lock_id(cve_id: str | None, jira_issue: str) -> str:
    """Derive the lock key segment from CVE id(s) or the Jira issue.

    Multiple CVEs are normalized to a sorted, comma-joined string so all
    sibling issues that share the same CVE set contend on one lock.
    """
    if cve_id and cve_id.strip():
        parts = sorted({p.strip().upper() for p in cve_id.replace(";", ",").split(",") if p.strip()})
        if parts:
            return ",".join(parts)
    return jira_issue.upper()


def _active_field(package: str, lock_id: str) -> str:
    return f"{package}:{lock_id}:active"


async def try_acquire_reproducer_lock(
    redis_conn,
    package: str,
    lock_id: str,
    jira_issue: str | None = None,
) -> str | None:
    """Acquire the create/adapt lock if no active holder exists.

    Returns the ownership token (serialized ``ReproducerLockEntry`` JSON) if
    this caller now holds the lock, or ``None`` if busy. Pass the token to
    ``release_reproducer_lock`` so a late release cannot delete a lock that
    another worker has since acquired.
    """
    field = _active_field(package, lock_id)
    entry = ReproducerLockEntry(
        package=package,
        lock_id=lock_id,
        jira_issue=jira_issue,
    )
    token = entry.model_dump_json()
    acquired = await fix_await(
        redis_conn.eval(
            _ACQUIRE_LUA,
            1,
            REPRODUCER_LOCK_HASH,
            field,
            token,
        )
    )
    if acquired:
        logger.info("Acquired reproducer lock for %s/%s", package, lock_id)
        return token
    logger.info("Reproducer lock busy for %s/%s", package, lock_id)
    return None


async def release_reproducer_lock(
    redis_conn,
    package: str,
    lock_id: str,
    token: str,
) -> bool:
    """Release the create/adapt lock only if *token* still owns it.

    Uses the same compare-and-delete Lua as the stale sweeper so a delayed
    ``finally`` from worker A cannot wipe worker B's re-acquired lock.
    Returns True if this call deleted the lock entry.
    """
    field = _active_field(package, lock_id)
    deleted = await fix_await(redis_conn.eval(_CONDITIONAL_HDEL_LUA, 1, REPRODUCER_LOCK_HASH, field, token))
    if deleted:
        logger.info("Released reproducer lock for %s/%s", package, lock_id)
        return True
    logger.warning(
        "Reproducer lock for %s/%s was not released — token no longer matches "
        "(already released, swept stale, or re-acquired by another worker)",
        package,
        lock_id,
    )
    return False


async def sweep_stale_reproducer_locks(
    redis_conn,
    threshold: timedelta = _DEFAULT_STALE_THRESHOLD,
) -> int:
    """Remove :active lock entries older than *threshold*.

    Uses compare-and-delete so a lock released and re-acquired between
    snapshot and delete is not wiped.
    """
    all_fields: dict = await fix_await(redis_conn.hgetall(REPRODUCER_LOCK_HASH))
    now = datetime.now(UTC)
    removed = 0

    for field, value in all_fields.items():
        field_str = field.decode() if isinstance(field, bytes) else field
        if not field_str.endswith(":active"):
            continue

        try:
            entry = ReproducerLockEntry.model_validate_json(value)
        except Exception:
            logger.warning("Cannot parse reproducer lock entry %s; skipping", field_str)
            continue

        age = now - entry.activated_at
        if age <= threshold:
            continue

        deleted = await fix_await(
            redis_conn.eval(_CONDITIONAL_HDEL_LUA, 1, REPRODUCER_LOCK_HASH, field, value)
        )
        if deleted:
            removed += 1
            logger.warning(
                "Removed stale reproducer lock %s (activated %s ago, threshold %s)",
                field_str,
                age,
                threshold,
            )

    return removed
