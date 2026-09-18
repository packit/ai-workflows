from __future__ import annotations

import enum
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


class SubmitResult(enum.Enum):
    """Outcome of :func:`submit_merge_job`."""

    SUBMITTED = "submitted"
    ALREADY_QUEUED = "already_queued"
    CONFLICT = "conflict"


# Lua script: atomic check-and-set for consolidation job submission.
#
# KEYS[1]  = hash key
# ARGV[1]  = pending field key
# ARGV[2]  = active field key
# ARGV[3]  = job JSON value
# ARGV[4]  = "strict" or "auto"
#
# In "strict" mode (label-triggered): fails if pending OR active exists.
# In "auto" mode: fails only if pending exists.
#
# Returns:
#   1  = submitted
#   0  = pending already exists  (already_queued / conflict depending on mode)
#  -1  = active exists           (conflict, strict mode only)
_SUBMIT_JOB_LUA = """
local hash    = KEYS[1]
local pending = ARGV[1]
local active  = ARGV[2]
local value   = ARGV[3]
local mode    = ARGV[4]

if redis.call('HEXISTS', hash, pending) == 1 then
    return 0
end

if mode == 'strict' and redis.call('HEXISTS', hash, active) == 1 then
    return -1
end

redis.call('HSET', hash, pending, value)
return 1
"""


async def submit_merge_job(
    redis_conn,
    package: str,
    target_branch: str,
    source_issues: list[str] | None = None,
    release_strategy: str | None = None,
) -> SubmitResult:
    """Atomically submit a merge consolidation job.

    Uses a Lua script so the existence check and the HSET are executed
    as a single atomic operation on the Redis server, eliminating the
    race where two concurrent requests both pass the check before
    either writes.

    When *source_issues* is set (label-triggered mode), the script
    runs in **strict** mode: submission is rejected if *either* a
    pending or an active job already exists for the same
    package/branch.

    When *source_issues* is None (auto mode), only an existing
    pending job blocks submission — an active job is allowed because
    auto-mode jobs are safe to queue behind a running one.

    Returns:
        :attr:`SubmitResult.SUBMITTED` if a new pending job was created.
        :attr:`SubmitResult.ALREADY_QUEUED` if a pending job already exists.
        :attr:`SubmitResult.CONFLICT` if an active (or pending) job blocks
        a label-triggered submission.
    """
    pending_key = _consolidation_field_key(package, target_branch, "pending")
    active_key = _consolidation_field_key(package, target_branch, "active")
    mode = "strict" if source_issues is not None else "auto"

    job = MergeConsolidationJob(
        package=package,
        target_branch=target_branch,
        active=False,
        submitted_at=datetime.now(UTC),
        source_issues=source_issues,
        release_strategy=release_strategy,
    )

    result = await fix_await(
        redis_conn.eval(
            _SUBMIT_JOB_LUA,
            1,
            _CONSOLIDATION_HASH_KEY,
            pending_key,
            active_key,
            job.model_dump_json(),
            mode,
        )
    )

    if result == 1:
        logger.info("Filed pending merge job for %s/%s (mode=%s)", package, target_branch, mode)
        return SubmitResult.SUBMITTED
    if result == -1:
        logger.info("Conflict: active job exists for %s/%s", package, target_branch)
        return SubmitResult.CONFLICT

    logger.info("Pending merge job already exists for %s/%s, skipping", package, target_branch)
    if mode == "strict":
        return SubmitResult.CONFLICT
    return SubmitResult.ALREADY_QUEUED


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
