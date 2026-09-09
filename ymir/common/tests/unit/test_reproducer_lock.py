"""Unit tests for reproducer create/adapt Redis lock."""

from datetime import UTC, datetime, timedelta

import pytest
from flexmock import flexmock

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

    async def _mock_fetch(issue_key: str) -> list[dict]:
        return chain[issue_key.upper()]

    assert await resolve_clone_root("RHEL-300", _mock_fetch) == "RHEL-100"
    assert await resolve_clone_root("RHEL-200", _mock_fetch) == "RHEL-100"
    assert await resolve_clone_root("RHEL-100", _mock_fetch) == "RHEL-100"


@pytest.mark.asyncio
async def test_resolve_reproducer_lock_id_uses_clone_root_for_bugs():
    chain = {
        "RHEL-100": [],
        "RHEL-200": [_cloners_link("RHEL-100", "RHEL-200")],
    }

    async def _mock_fetch(issue_key: str) -> list[dict]:
        return chain[issue_key.upper()]

    lock_id = await resolve_reproducer_lock_id(
        None,
        "RHEL-200",
        fetch_issuelinks=_mock_fetch,
    )
    assert lock_id == "RHEL-100"


@pytest.mark.asyncio
async def test_resolve_reproducer_lock_id_skips_clone_walk_for_cve():
    async def _mock_fetch(_issue: str):
        pytest.fail("fetch_issuelinks should not have been called")

    lock_id = await resolve_reproducer_lock_id(
        "CVE-2026-1",
        "RHEL-200",
        fetch_issuelinks=_mock_fetch,
    )
    assert lock_id == "CVE-2026-1"


@pytest.mark.asyncio
async def test_resolve_reproducer_lock_id_falls_back_on_fetch_error():
    async def _fetch_failing(_issue: str) -> list[dict]:
        raise RuntimeError("jira down")

    lock_id = await resolve_reproducer_lock_id(
        None,
        "RHEL-200",
        fetch_issuelinks=_fetch_failing,
    )
    assert lock_id == "RHEL-200"


@pytest.mark.asyncio
async def test_try_acquire_reproducer_lock_success():
    args = []

    async def _mock_eval(*_args, **_kwargs):
        nonlocal args
        args = _args
        return 1

    redis = flexmock()
    redis.should_receive("eval").replace_with(_mock_eval).once()

    token = await try_acquire_reproducer_lock(redis, "bind", "CVE-1", jira_issue="RHEL-1")
    assert token is not None
    entry = ReproducerLockEntry.model_validate_json(token)
    assert entry.package == "bind"
    assert entry.lock_id == "CVE-1"
    assert entry.jira_issue == "RHEL-1"

    assert args[2] == REPRODUCER_LOCK_HASH
    assert args[3] == "bind:CVE-1:active"
    assert args[4] == token


@pytest.mark.asyncio
async def test_try_acquire_reproducer_lock_busy():
    async def _mock_eval(*_args, **_kwargs):
        return 0

    redis = flexmock()
    redis.should_receive("eval").replace_with(_mock_eval)

    assert await try_acquire_reproducer_lock(redis, "bind", "CVE-1") is None


@pytest.mark.asyncio
async def test_release_reproducer_lock_compare_and_delete():
    args = []

    async def _mock_eval(*_args, **_kwargs):
        nonlocal args
        args = _args
        return 1

    async def _mock_lpop(*_args, **_kwargs):
        return None

    redis = flexmock()
    redis.should_receive("eval").replace_with(_mock_eval).once()
    redis.should_receive("hdel").never()
    redis.should_receive("lpop").with_args(blocked_reproducer_queue_key("bind", "CVE-1")).replace_with(
        _mock_lpop
    ).once()
    token = ReproducerLockEntry(package="bind", lock_id="CVE-1", jira_issue="RHEL-1").model_dump_json()

    assert await release_reproducer_lock(redis, "bind", "CVE-1", token) is True
    assert "HGET" in args[0] and "HDEL" in args[0]
    assert args[2] == REPRODUCER_LOCK_HASH
    assert args[3] == "bind:CVE-1:active"
    assert args[4] == token


async def _async_noop(*_args, **_kwargs):
    pass


@pytest.mark.asyncio
async def test_promote_blocked_reproducer_tasks():
    payload = '{"metadata":{"jira_issue":"RHEL-2","package":"bind"},"attempts":0,"user_triggered":false}'

    lpop_returns = iter([payload.encode(), None])

    async def _mock_lpop(*_args, **_kwargs):
        return next(lpop_returns)

    redis = flexmock()
    redis.should_receive("lpop").replace_with(_mock_lpop)
    redis.should_receive("lpush").with_args("reproducer_queue", payload).once().replace_with(_async_noop)

    promoted = await promote_blocked_reproducer_tasks(redis, "bind", "CVE-1")
    assert promoted == 1


@pytest.mark.asyncio
async def test_enqueue_blocked_reproducer_task():
    payload = '{"metadata":{"jira_issue":"RHEL-2","package":"bind"}}'

    redis = flexmock()
    redis.should_receive("rpush").with_args(
        blocked_reproducer_queue_key("bind", "CVE-1"), payload
    ).replace_with(_async_noop).once()

    await enqueue_blocked_reproducer_task(redis, "bind", "CVE-1", payload)


@pytest.mark.asyncio
async def test_release_reproducer_lock_skips_when_token_mismatch():
    """A late finally must not wipe a lock re-acquired by another worker."""

    async def _mock_eval(*_args, **_kwargs):
        return 0

    redis = flexmock()
    redis.should_receive("eval").replace_with(_mock_eval).once()
    redis.should_receive("hdel").never()

    stale_token = ReproducerLockEntry(
        package="bind",
        lock_id="CVE-1",
        jira_issue="RHEL-OLD",
        activated_at=datetime.now(UTC) - timedelta(hours=7),
    ).model_dump_json()

    assert await release_reproducer_lock(redis, "bind", "CVE-1", stale_token) is False


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

    async def _mock_hgetall(*_args, **_kwargs):
        return {
            b"bind:CVE-1:active": stale.model_dump_json().encode(),
            b"bind:CVE-2:active": fresh.model_dump_json().encode(),
        }

    async def _mock_eval(*_args, **_kwargs):
        return 1

    async def _mock_lpop(*_args, **_kwargs):
        return None

    redis = flexmock()
    redis.should_receive("hgetall").replace_with(_mock_hgetall)
    redis.should_receive("eval").replace_with(_mock_eval).once()
    redis.should_receive("lpop").replace_with(_mock_lpop)

    removed = await sweep_stale_reproducer_locks(redis, threshold=timedelta(hours=6))
    assert removed == 1
