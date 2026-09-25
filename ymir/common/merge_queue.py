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


def _heartbeat_field_key(package: str, branch: str) -> str:
    return _consolidation_field_key(package, branch, "heartbeat")


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
# ARGV[5]  = recovery field key
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
local recovery = ARGV[5]

if redis.call('HEXISTS', hash, pending) == 1 then
    return 0
end

local recovery_prefix_exists = false
if mode == 'strict' then
    recovery_prefix_exists = redis.call('HEXISTS', hash, recovery) == 1
    if not recovery_prefix_exists then
        local fields = redis.call('HKEYS', hash)
        for _, field in ipairs(fields) do
            if string.sub(field, 1, #recovery + 1) == recovery .. ':' then
                recovery_prefix_exists = true
                break
            end
        end
    end
end

if mode == 'strict' and (redis.call('HEXISTS', hash, active) == 1 or
                         recovery_prefix_exists) then
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
    recovery_key = _consolidation_field_key(package, target_branch, "recovery")
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
            recovery_key,
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
-- Recovery jobs take precedence over newly submitted jobs.  A recovery field
-- can coexist with :pending when shutdown interrupted an active job.
for _, suffix in ipairs({':recovery', ':pending'}) do
    for i = 1, #fields, 2 do
        local field = fields[i]
        local value = fields[i + 1]
        local prefix
        if suffix == ':recovery' and string.sub(field, 1, #field):find(':recovery:', 1, true) then
            prefix = string.sub(field, 1, string.find(field, ':recovery:', 1, true) - 1)
        elseif string.sub(field, -#suffix) == suffix then
            prefix = string.sub(field, 1, #field - #suffix)
        end
        if prefix then
            local active_key = prefix .. ':active'
            if redis.call('HEXISTS', hash, active_key) == 0 then
                redis.call('HDEL', hash, field)
                redis.call('HSET', hash, active_key, value)
                local now = redis.call('TIME')
                redis.call('HSET', hash, prefix .. ':heartbeat', now[1] .. '.' .. now[2])
                return {field, value, active_key}
            end
        end
    end
end
return nil
"""


async def pick_next_job(redis_conn) -> MergeConsolidationJob | None:
    """Pick the next recovery or pending consolidation job and promote it to active.

    Uses a Lua script so the scan-check-promote is atomic on the Redis
    server, preventing two workers from picking the same job.

    After atomic promotion, writes back the updated JSON with
    ``activated_at`` set so the staleness sweep can measure genuine
    active-time rather than total queue-time (submitted_at includes
    time spent waiting in :pending).

    Returns:
        The activated job, or None if no queued jobs exist.
    """
    result = await fix_await(redis_conn.eval(_PICK_JOB_LUA, 1, _CONSOLIDATION_HASH_KEY))
    if result is None:
        return None

    _field, value, active_key = result
    job = MergeConsolidationJob.model_validate_json(value)
    job.active = True
    job.activated_at = datetime.now(UTC)

    active_key = active_key.decode() if isinstance(active_key, bytes) else active_key
    updated = await fix_await(
        redis_conn.eval(
            _UPDATE_ACTIVE_LUA,
            1,
            _CONSOLIDATION_HASH_KEY,
            active_key,
            value,
            job.model_dump_json(),
        )
    )
    if not updated:
        # A stale sweep may have moved the value after promotion but before
        # this timestamp update. Let the next poll pick the job again rather
        # than processing a payload that is also in recovery storage.
        logger.info("Active job changed during promotion; retrying next poll")
        return None

    logger.info("Promoted pending job to active for %s/%s", job.package, job.target_branch)
    return job


_UPDATE_ACTIVE_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
    return 1
end
return 0
"""


_REQUEUE_ACTIVE_LUA = """
local hash = KEYS[1]
local active = ARGV[1]
local recovery = ARGV[2]
local expected = ARGV[3]
local current = redis.call('HGET', hash, active)
if current ~= expected then
    return 0
end
redis.call('HDEL', hash, active)
local recovery_key = recovery
if redis.call('HEXISTS', hash, recovery_key) == 1 then
    recovery_key = recovery .. ':' .. redis.call('INCR', hash .. ':recovery_seq')
end
redis.call('HSET', hash, recovery_key, expected)
redis.call('HDEL', hash, string.gsub(active, ':active$', ':heartbeat'))
return 1
"""


async def requeue_active_job(
    redis_conn,
    package: str,
    target_branch: str,
    expected_value: str | bytes,
) -> bool:
    """Move the caller's active job to durable recovery storage."""
    active_key = _consolidation_field_key(package, target_branch, "active")
    recovery_key = _consolidation_field_key(package, target_branch, "recovery")
    moved = await fix_await(
        redis_conn.eval(
            _REQUEUE_ACTIVE_LUA,
            1,
            _CONSOLIDATION_HASH_KEY,
            active_key,
            recovery_key,
            expected_value,
        )
    )
    if moved:
        logger.info("Requeued interrupted merge job for %s/%s", package, target_branch)
    return bool(moved)


_HEARTBEAT_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current ~= ARGV[2] then
    return 0
end
local now = redis.call('TIME')
redis.call('HSET', KEYS[1], ARGV[3], now[1] .. '.' .. now[2])
return 1
"""


async def refresh_active_heartbeat(
    redis_conn,
    package: str,
    target_branch: str,
    expected_value: str | bytes,
) -> bool:
    """Refresh the active job lease while proving ownership."""
    active_key = _consolidation_field_key(package, target_branch, "active")
    heartbeat_key = _heartbeat_field_key(package, target_branch)
    refreshed = await fix_await(
        redis_conn.eval(
            _HEARTBEAT_LUA,
            1,
            _CONSOLIDATION_HASH_KEY,
            active_key,
            expected_value,
            heartbeat_key,
        )
    )
    return bool(refreshed)


async def complete_job(
    redis_conn,
    package: str,
    target_branch: str,
    expected_value: str | bytes,
) -> bool:
    """Remove the caller-owned active consolidation job.

    Args:
        redis_conn: Active Redis connection.
        package: RPM package name.
        target_branch: Dist-git target branch.
        expected_value: Serialized payload owned by the caller.
    """
    active_key = _consolidation_field_key(package, target_branch, "active")
    heartbeat_key = _heartbeat_field_key(package, target_branch)
    completed = await fix_await(
        redis_conn.eval(
            _COMPLETE_JOB_LUA,
            1,
            _CONSOLIDATION_HASH_KEY,
            active_key,
            expected_value,
            heartbeat_key,
        )
    )
    if completed:
        logger.info("Completed active merge job for %s/%s", package, target_branch)
    else:
        logger.warning(
            "Skipped completing merge job for %s/%s: active payload is owned by another worker",
            package,
            target_branch,
        )
    return bool(completed)


_CONDITIONAL_HDEL_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
local heartbeat = redis.call('HGET', KEYS[1], ARGV[4])
if current == ARGV[2] and (ARGV[5] == '' or heartbeat == ARGV[5]) then
    redis.call('HDEL', KEYS[1], ARGV[1])
    redis.call('HDEL', KEYS[1], ARGV[4])
    local recovery_key = ARGV[3]
    if redis.call('HEXISTS', KEYS[1], recovery_key) == 1 then
        recovery_key = recovery_key .. ':' .. redis.call('INCR', KEYS[1] .. ':recovery_seq')
    end
    redis.call('HSET', KEYS[1], recovery_key, ARGV[2])
    return 1
end
return 0
"""


_COMPLETE_JOB_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    redis.call('HDEL', KEYS[1], ARGV[1])
    redis.call('HDEL', KEYS[1], ARGV[3])
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
    field is moved to :recovery (to avoid silent data loss).  A hard crash can
    leave it in :active, though, and pick_next_job refuses to promote a
    :pending job while :active exists for the same package/branch.  This sweep
    recovers such entries after a conservative timeout.

    This sweep runs on every poll cycle, recovering :active entries that have
    been sitting untouched for longer than *threshold*.  The default
    of 6 hours is well above the longest observed consolidation run
    (~1-2 hours) but short enough to self-heal within the same workday.

    Staleness is measured from the renewable heartbeat written by
    ``pick_next_job`` and refreshed by the worker, not ``submitted_at``.
    Entries from before heartbeat support fall back to ``activated_at``.

    Recovery uses an atomic compare-and-move Lua script: the stored
    value must still byte-match the snapshot read by HGETALL.  If
    ``complete_job`` removed the entry and ``pick_next_job`` promoted a
    fresh one into the same field between the snapshot and the move, the
    values will differ and the move is skipped.

    Returns the number of stale entries recovered.
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

        heartbeat_field = f"{field_str.removesuffix(':active')}:heartbeat"
        heartbeat_value = all_fields.get(heartbeat_field.encode())
        if heartbeat_value is None:
            heartbeat_value = all_fields.get(heartbeat_field)

        if heartbeat_value is not None:
            try:
                heartbeat_timestamp = float(
                    heartbeat_value.decode() if isinstance(heartbeat_value, bytes) else heartbeat_value
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Cannot parse heartbeat for :active entry %s; skipping staleness check", field_str
                )
                continue
            age = timedelta(seconds=now.timestamp() - heartbeat_timestamp)
        elif job.activated_at is not None:
            age = now - job.activated_at
        else:
            logger.info(
                "Skipping :active entry %s with no heartbeat or activated_at",
                field_str,
            )
            continue

        if age <= threshold:
            continue

        recovery_field = f"{field_str.removesuffix(':active')}:recovery"
        heartbeat_expected = heartbeat_value or b""
        deleted = await fix_await(
            redis_conn.eval(
                _CONDITIONAL_HDEL_LUA,
                1,
                _CONSOLIDATION_HASH_KEY,
                field,
                value,
                recovery_field,
                heartbeat_field.encode(),
                heartbeat_expected,
            )
        )
        if deleted:
            removed += 1
            logger.warning(
                "Requeued stale :active consolidation entry %s (activated %s ago, threshold %s)",
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
