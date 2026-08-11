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

    token = await try_acquire_reproducer_lock(redis, "bind", "CVE-1", jira_issue="RHEL-1")
    assert token is not None
    entry = ReproducerLockEntry.model_validate_json(token)
    assert entry.package == "bind"
    assert entry.lock_id == "CVE-1"
    assert entry.jira_issue == "RHEL-1"

    redis.eval.assert_awaited_once()
    args = redis.eval.await_args.args
    assert args[2] == REPRODUCER_LOCK_HASH
    assert args[3] == "bind:CVE-1:active"
    assert args[4] == token


@pytest.mark.asyncio
async def test_try_acquire_reproducer_lock_busy():
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=0)

    assert await try_acquire_reproducer_lock(redis, "bind", "CVE-1") is None


@pytest.mark.asyncio
async def test_release_reproducer_lock_compare_and_delete():
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    token = ReproducerLockEntry(package="bind", lock_id="CVE-1", jira_issue="RHEL-1").model_dump_json()

    assert await release_reproducer_lock(redis, "bind", "CVE-1", token) is True
    redis.eval.assert_awaited_once()
    args = redis.eval.await_args.args
    assert "HGET" in args[0] and "HDEL" in args[0]
    assert args[2] == REPRODUCER_LOCK_HASH
    assert args[3] == "bind:CVE-1:active"
    assert args[4] == token
    redis.hdel.assert_not_called()


@pytest.mark.asyncio
async def test_release_reproducer_lock_skips_when_token_mismatch():
    """A late finally must not wipe a lock re-acquired by another worker."""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=0)
    stale_token = ReproducerLockEntry(
        package="bind",
        lock_id="CVE-1",
        jira_issue="RHEL-OLD",
        activated_at=datetime.now(UTC) - timedelta(hours=7),
    ).model_dump_json()

    assert await release_reproducer_lock(redis, "bind", "CVE-1", stale_token) is False
    redis.eval.assert_awaited_once()
    redis.hdel.assert_not_called()


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
