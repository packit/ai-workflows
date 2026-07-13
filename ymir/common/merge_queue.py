from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ymir.common.base_utils import fix_await
from ymir.common.constants import RedisQueues
from ymir.common.models import MergeConsolidationJob

logger = logging.getLogger(__name__)

_CONSOLIDATION_HASH_KEY = RedisQueues.MERGE_CONSOLIDATION_QUEUE.value
_DEFAULT_STALE_ACTIVE_THRESHOLD = timedelta(hours=6)


def _consolidation_field_key(package: str, branch: str, slot: str) -> str:
    return f"{package}:{branch}:{slot}"


async def submit_merge_job(
    redis_conn,
    package: str,
    target_branch: str,
    source_issues: list[str] | None = None,
    release_strategy: str | None = None,
) -> bool:
    """Submit a merge consolidation job if the queue invariant allows it.

    The consolidation queue uses a Redis Hash with at-most-one-active and
    one-pending entry per package-branch pair.

    Args:
        redis_conn: Active Redis connection.
        package: RPM package name.
        target_branch: Dist-git target branch.
        source_issues: When set, the consolidation agent will target only
            MRs for these specific Jira issue keys (label-triggered mode).
            When None, it picks the two oldest open MRs (auto mode).

    Returns:
        True if a new pending job was created, False if one already exists
        or is unnecessary.
    """
    pending_key = _consolidation_field_key(package, target_branch, "pending")
    active_key = _consolidation_field_key(package, target_branch, "active")

    existing_pending = await fix_await(redis_conn.hget(_CONSOLIDATION_HASH_KEY, pending_key))
    if existing_pending is not None:
        logger.info(
            "Pending merge job already exists for %s/%s, skipping",
            package,
            target_branch,
        )
        return False

    existing_active = await fix_await(redis_conn.hget(_CONSOLIDATION_HASH_KEY, active_key))

    job = MergeConsolidationJob(
        package=package,
        target_branch=target_branch,
        active=False,
        submitted_at=datetime.now(UTC),
        source_issues=source_issues,
        release_strategy=release_strategy,
    )
    await fix_await(redis_conn.hset(_CONSOLIDATION_HASH_KEY, pending_key, job.model_dump_json()))

    if existing_active is not None:
        logger.info("Active job running for %s/%s; filed pending job", package, target_branch)
    else:
        logger.info("No active job for %s/%s; filed pending job", package, target_branch)
    return True


# Lua script ensures the scan-check-promote is atomic on the Redis server,
# preventing two concurrent workers from picking the same pending job.
_PICK_JOB_LUA = """
local hash = KEYS[1]
local fields = redis.call('HGETALL', hash)
for i = 1, #fields, 2 do
    local field = fields[i]
    local value = fields[i + 1]
    if string.sub(field, -8) == ':pending' then
        local prefix = string.sub(field, 1, #field - 8)
        local active_key = prefix .. ':active'
        if redis.call('HEXISTS', hash, active_key) == 0 then
            redis.call('HDEL', hash, field)
            redis.call('HSET', hash, active_key, value)
            return {field, value}
        end
    end
end
return nil
"""


async def pick_next_job(redis_conn) -> MergeConsolidationJob | None:
    """Pick the next pending consolidation job and promote it to active.

    Uses a Lua script so the scan-check-promote is atomic on the Redis
    server, preventing two workers from picking the same job.

    After atomic promotion, writes back the updated JSON with
    ``activated_at`` set so the staleness sweep can measure genuine
    active-time rather than total queue-time (submitted_at includes
    time spent waiting in :pending).

    Returns:
        The activated job, or None if no pending jobs exist.
    """
    result = await fix_await(redis_conn.eval(_PICK_JOB_LUA, 1, _CONSOLIDATION_HASH_KEY))
    if result is None:
        return None

    field, value = result
    job = MergeConsolidationJob.model_validate_json(value)
    job.active = True
    job.activated_at = datetime.now(UTC)

    field_str = field.decode() if isinstance(field, bytes) else field
    active_key = field_str.removesuffix(":pending") + ":active"
    await fix_await(redis_conn.hset(_CONSOLIDATION_HASH_KEY, active_key, job.model_dump_json()))

    logger.info("Promoted pending job to active for %s/%s", job.package, job.target_branch)
    return job


async def complete_job(
    redis_conn,
    package: str,
    target_branch: str,
) -> None:
    """Remove the active consolidation job for a package-branch pair.

    Args:
        redis_conn: Active Redis connection.
        package: RPM package name.
        target_branch: Dist-git target branch.
    """
    active_key = _consolidation_field_key(package, target_branch, "active")
    await fix_await(redis_conn.hdel(_CONSOLIDATION_HASH_KEY, active_key))
    logger.info("Completed active merge job for %s/%s", package, target_branch)


_CONDITIONAL_HDEL_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    redis.call('HDEL', KEYS[1], ARGV[1])
    return 1
end
return 0
"""


async def sweep_stale_active_jobs(
    redis_conn,
    threshold: timedelta = _DEFAULT_STALE_ACTIVE_THRESHOLD,
) -> int:
    """Remove :active entries whose ``activated_at`` is older than *threshold*.

    When a consolidation task is cancelled by shutdown, its :active hash
    field is deliberately left in place (to avoid silent data loss).  But
    pick_next_job's Lua script refuses to promote a :pending job while
    :active exists for the same package/branch — so without periodic
    cleanup, a redeploy that interrupts a consolidation job permanently
    blocks future consolidation for that package/branch.

    This sweep runs on every poll cycle, deleting :active entries that
    have been sitting untouched for longer than *threshold*.  The default
    of 6 hours is well above the longest observed consolidation run
    (~1-2 hours) but short enough to self-heal within the same workday.

    Staleness is measured from ``activated_at`` (set by ``pick_next_job``
    at promotion time), not ``submitted_at`` (set at initial queueing).
    This avoids falsely sweeping a job that sat in :pending for a while
    before being promoted — its genuine active-time may be much shorter
    than its total queue-time.

    Deletion uses an atomic compare-and-delete Lua script: the stored
    value must still byte-match the snapshot read by HGETALL.  If
    ``complete_job`` removed the entry and ``pick_next_job`` promoted a
    fresh one into the same field between the snapshot and the delete,
    the values will differ and the delete is skipped.

    Returns the number of stale entries removed.
    """
    all_fields: dict[bytes, bytes] = await fix_await(redis_conn.hgetall(_CONSOLIDATION_HASH_KEY))
    now = datetime.now(UTC)
    removed = 0

    for field, value in all_fields.items():
        field_str = field.decode() if isinstance(field, bytes) else field
        if not field_str.endswith(":active"):
            continue

        try:
            job = MergeConsolidationJob.model_validate_json(value)
        except Exception:
            logger.warning(
                "Cannot parse :active entry %s; skipping staleness check",
                field_str,
            )
            continue

        if job.activated_at is None:
            logger.info(
                "Skipping :active entry %s with no activated_at (promoted before sweep support was deployed)",
                field_str,
            )
            continue

        age = now - job.activated_at
        if age <= threshold:
            continue

        deleted = await fix_await(
            redis_conn.eval(_CONDITIONAL_HDEL_LUA, 1, _CONSOLIDATION_HASH_KEY, field, value)
        )
        if deleted:
            removed += 1
            logger.warning(
                "Removed stale :active consolidation entry %s (activated %s ago, threshold %s)",
                field_str,
                age,
                threshold,
            )
        else:
            logger.info(
                "Skipped stale :active entry %s — value changed since snapshot "
                "(likely completed and re-promoted concurrently)",
                field_str,
            )

    return removed
