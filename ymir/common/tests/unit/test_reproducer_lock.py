"""Unit tests for reproducer create/adapt Redis lock."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from ymir.common.reproducer_lock import (
    REPRODUCER_LOCK_HASH,
    ReproducerLockEntry,
    release_reproducer_lock,
    reproducer_lock_id,
    sweep_stale_reproducer_locks,
    try_acquire_reproducer_lock,
)


@pytest.mark.parametrize(
    ("cve_id", "jira_issue", "expected"),
    [
        ("CVE-2025-1", "RHEL-1", "CVE-2025-1"),
        ("cve-2025-2, CVE-2025-1", "RHEL-1", "CVE-2025-1,CVE-2025-2"),
        (None, "RHEL-99", "RHEL-99"),
        ("", "rhel-99", "RHEL-99"),
        ("  ", "RHEL-99", "RHEL-99"),
    ],
)
def test_reproducer_lock_id(cve_id, jira_issue, expected):
    assert reproducer_lock_id(cve_id, jira_issue) == expected


@pytest.mark.asyncio
async def test_try_acquire_reproducer_lock_success():
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)

    assert await try_acquire_reproducer_lock(redis, "bind", "CVE-1", jira_issue="RHEL-1") is True
    redis.eval.assert_awaited_once()
    args = redis.eval.await_args.args
    assert args[2] == REPRODUCER_LOCK_HASH
    assert args[3] == "bind:CVE-1:active"


@pytest.mark.asyncio
async def test_try_acquire_reproducer_lock_busy():
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=0)

    assert await try_acquire_reproducer_lock(redis, "bind", "CVE-1") is False


@pytest.mark.asyncio
async def test_release_reproducer_lock():
    redis = MagicMock()
    redis.hdel = AsyncMock(return_value=1)

    await release_reproducer_lock(redis, "bind", "CVE-1")
    redis.hdel.assert_awaited_once_with(REPRODUCER_LOCK_HASH, "bind:CVE-1:active")


@pytest.mark.asyncio
async def test_sweep_stale_reproducer_locks_removes_old():
    stale = ReproducerLockEntry(
        package="bind",
        lock_id="CVE-1",
        activated_at=datetime.now(UTC) - timedelta(hours=7),
    )
    fresh = ReproducerLockEntry(
        package="bind",
        lock_id="CVE-2",
        activated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    redis = MagicMock()
    redis.hgetall = AsyncMock(
        return_value={
            b"bind:CVE-1:active": stale.model_dump_json().encode(),
            b"bind:CVE-2:active": fresh.model_dump_json().encode(),
        }
    )
    redis.eval = AsyncMock(return_value=1)

    removed = await sweep_stale_reproducer_locks(redis, threshold=timedelta(hours=6))
    assert removed == 1
    redis.eval.assert_awaited_once()
