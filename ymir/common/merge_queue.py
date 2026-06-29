from __future__ import annotations

import logging
from datetime import UTC, datetime

from ymir.common.base_utils import fix_await
from ymir.common.constants import RedisQueues
from ymir.common.models import MergeConsolidationJob

logger = logging.getLogger(__name__)

_CONSOLIDATION_HASH_KEY = RedisQueues.MERGE_CONSOLIDATION_QUEUE.value


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

    Returns:
        The activated job, or None if no pending jobs exist.
    """
    result = await fix_await(redis_conn.eval(_PICK_JOB_LUA, 1, _CONSOLIDATION_HASH_KEY))
    if result is None:
        return None

    _field, value = result
    job = MergeConsolidationJob.model_validate_json(value)
    job.active = True
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
