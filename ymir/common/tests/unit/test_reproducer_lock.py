"""Unit tests for reproducer create/adapt Redis lock."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from ymir.common.reproducer_lock import (
    REPRODUCER_LOCK_HASH,
    ReproducerLockEntry,
    _immediate_clone_parent,
    blocked_reproducer_queue_key,
    enqueue_blocked_reproducer_task,
    promote_blocked_reproducer_tasks,
    release_reproducer_lock,
    reproducer_lock_id,
    resolve_clone_root,
    resolve_reproducer_lock_id,
    sweep_stale_reproducer_locks,
    try_acquire_reproducer_lock,
)


def _cloners_link(parent: str, clone: str) -> dict:
    return {
        "type": {"name": "Cloners", "inward": "is cloned by", "outward": "clones"},
        "inwardIssue": {"key": clone},
        "outwardIssue": {"key": parent},
    }


@pytest.mark.parametrize(
    ("cve_id", "jira_issue", "clone_root", "expected"),
    [
        ("CVE-2025-1", "RHEL-1", None, "CVE-2025-1"),
        ("cve-2025-2, CVE-2025-1", "RHEL-1", None, "CVE-2025-1,CVE-2025-2"),
        (None, "RHEL-99", None, "RHEL-99"),
        ("", "rhel-99", None, "RHEL-99"),
        ("  ", "RHEL-99", None, "RHEL-99"),
        (None, "RHEL-200", "RHEL-100", "RHEL-100"),
        ("CVE-2025-1", "RHEL-200", "RHEL-100", "CVE-2025-1"),
    ],
)
def test_reproducer_lock_id(cve_id, jira_issue, clone_root, expected):
    assert reproducer_lock_id(cve_id, jira_issue, clone_root=clone_root) == expected


def test_immediate_clone_parent_finds_cloner():
    links = [_cloners_link("RHEL-100", "RHEL-200")]
    assert _immediate_clone_parent("RHEL-200", links) == "RHEL-100"


def test_immediate_clone_parent_ignores_outward_clone_direction():
    links = [_cloners_link("RHEL-100", "RHEL-200")]
    assert _immediate_clone_parent("RHEL-100", links) is None


def test_immediate_clone_parent_picks_smallest_when_multiple():
    links = [
        _cloners_link("RHEL-100", "RHEL-300"),
        _cloners_link("RHEL-200", "RHEL-300"),
    ]
    assert _immediate_clone_parent("RHEL-300", links) == "RHEL-100"


@pytest.mark.asyncio
async def test_resolve_clone_root_walks_chain():
    chain = {
        "RHEL-100": [],
        "RHEL-200": [_cloners_link("RHEL-100", "RHEL-200")],
        "RHEL-300": [_cloners_link("RHEL-200", "RHEL-300")],
    }

    async def fetch(issue_key: str) -> list[dict]:
        return chain[issue_key.upper()]

    assert await resolve_clone_root("RHEL-300", fetch) == "RHEL-100"
    assert await resolve_clone_root("RHEL-200", fetch) == "RHEL-100"
    assert await resolve_clone_root("RHEL-100", fetch) == "RHEL-100"


@pytest.mark.asyncio
async def test_resolve_reproducer_lock_id_uses_clone_root_for_bugs():
    chain = {
        "RHEL-100": [],
        "RHEL-200": [_cloners_link("RHEL-100", "RHEL-200")],
    }

    async def fetch(issue_key: str) -> list[dict]:
        return chain[issue_key.upper()]

    lock_id = await resolve_reproducer_lock_id(
        None,
        "RHEL-200",
        fetch_issuelinks=fetch,
    )
    assert lock_id == "RHEL-100"


@pytest.mark.asyncio
async def test_resolve_reproducer_lock_id_skips_clone_walk_for_cve():
    fetch = AsyncMock()
    lock_id = await resolve_reproducer_lock_id(
        "CVE-2026-1",
        "RHEL-200",
        fetch_issuelinks=fetch,
    )
    assert lock_id == "CVE-2026-1"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_reproducer_lock_id_falls_back_on_fetch_error():
    fetch = AsyncMock(side_effect=RuntimeError("jira down"))
    lock_id = await resolve_reproducer_lock_id(
        None,
        "RHEL-200",
        fetch_issuelinks=fetch,
    )
    assert lock_id == "RHEL-200"


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
    redis.lpop = AsyncMock(return_value=None)
    token = ReproducerLockEntry(package="bind", lock_id="CVE-1", jira_issue="RHEL-1").model_dump_json()

    assert await release_reproducer_lock(redis, "bind", "CVE-1", token) is True
    redis.eval.assert_awaited_once()
    args = redis.eval.await_args.args
    assert "HGET" in args[0] and "HDEL" in args[0]
    assert args[2] == REPRODUCER_LOCK_HASH
    assert args[3] == "bind:CVE-1:active"
    assert args[4] == token
    redis.hdel.assert_not_called()
    redis.lpop.assert_awaited_once_with(blocked_reproducer_queue_key("bind", "CVE-1"))


@pytest.mark.asyncio
async def test_promote_blocked_reproducer_tasks():
    payload = '{"metadata":{"jira_issue":"RHEL-2","package":"bind"},"attempts":0,"user_triggered":false}'
    redis = MagicMock()
    redis.lpop = AsyncMock(side_effect=[payload.encode(), None])
    redis.lpush = AsyncMock()

    promoted = await promote_blocked_reproducer_tasks(redis, "bind", "CVE-1")
    assert promoted == 1
    redis.lpush.assert_awaited_once_with("reproducer_queue", payload)


@pytest.mark.asyncio
async def test_enqueue_blocked_reproducer_task():
    redis = MagicMock()
    redis.rpush = AsyncMock()
    payload = '{"metadata":{"jira_issue":"RHEL-2","package":"bind"}}'

    await enqueue_blocked_reproducer_task(redis, "bind", "CVE-1", payload)
    redis.rpush.assert_awaited_once_with(blocked_reproducer_queue_key("bind", "CVE-1"), payload)


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
    redis.lpop = AsyncMock(return_value=None)

    removed = await sweep_stale_reproducer_locks(redis, threshold=timedelta(hours=6))
    assert removed == 1
    redis.eval.assert_awaited_once()
