import asyncio
import contextlib

import pytest

from ymir.common.issue_lock import (
    _CONDITIONAL_DEL_LUA,
    _CONDITIONAL_PEXPIRE_LUA,
    _heartbeat_loop,
    acquire_issue_lock,
    extend_issue_lock,
    issue_lock,
    release_issue_lock,
)


class FakeRedis:
    """Minimal stand-in for redis.asyncio.Redis covering the lock primitives.

    Simulates SET NX PX, GET, DEL, PEXPIRE, and EVAL for the two Lua scripts
    used by issue_lock.py.
    """

    def __init__(self):
        self._store: dict[str, str] = {}
        self.eval_calls: list[tuple[str, int, tuple]] = []

    async def set(self, name, value, *, nx=False, px=None):
        if nx and name in self._store:
            return None
        self._store[name] = value
        return True

    async def get(self, name):
        return self._store.get(name)

    async def eval(self, script, numkeys, *args):
        self.eval_calls.append((script, numkeys, args))
        if script == _CONDITIONAL_DEL_LUA:
            key = args[0]
            token = args[1]
            current = self._store.get(key)
            if current == token:
                del self._store[key]
                return 1
            return 0
        if script == _CONDITIONAL_PEXPIRE_LUA:
            key = args[0]
            token = args[1]
            current = self._store.get(key)
            if current == token:
                return 1
            return 0
        return None

    def inject(self, key: str, value: str) -> None:
        """Directly set a key for test setup."""
        self._store[key] = value


async def _wait_until(predicate, timeout=2.0, interval=0.005):
    async def _poll():
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


class TestAcquireIssueLock:
    @pytest.mark.asyncio
    async def test_acquire_succeeds_on_empty_key(self):
        fake = FakeRedis()
        assert await acquire_issue_lock(fake, "RHEL-123", "tok-1") is True
        assert fake._store["lock:triage:RHEL-123"] == "tok-1"

    @pytest.mark.asyncio
    async def test_acquire_fails_when_already_held(self):
        fake = FakeRedis()
        assert await acquire_issue_lock(fake, "RHEL-123", "tok-1") is True
        assert await acquire_issue_lock(fake, "RHEL-123", "tok-2") is False
        assert fake._store["lock:triage:RHEL-123"] == "tok-1"

    @pytest.mark.asyncio
    async def test_acquire_succeeds_after_expiry(self):
        fake = FakeRedis()
        assert await acquire_issue_lock(fake, "RHEL-123", "tok-1") is True
        # Simulate expiry by removing the key
        del fake._store["lock:triage:RHEL-123"]
        assert await acquire_issue_lock(fake, "RHEL-123", "tok-2") is True
        assert fake._store["lock:triage:RHEL-123"] == "tok-2"


class TestReleaseIssueLock:
    @pytest.mark.asyncio
    async def test_release_succeeds_with_matching_token(self):
        fake = FakeRedis()
        fake.inject("lock:triage:RHEL-123", "tok-1")
        assert await release_issue_lock(fake, "RHEL-123", "tok-1") is True
        assert "lock:triage:RHEL-123" not in fake._store

    @pytest.mark.asyncio
    async def test_release_fails_with_wrong_token(self):
        fake = FakeRedis()
        fake.inject("lock:triage:RHEL-123", "tok-1")
        assert await release_issue_lock(fake, "RHEL-123", "tok-WRONG") is False
        assert fake._store["lock:triage:RHEL-123"] == "tok-1"

    @pytest.mark.asyncio
    async def test_release_fails_on_expired_key(self):
        fake = FakeRedis()
        assert await release_issue_lock(fake, "RHEL-123", "tok-1") is False


class TestExtendIssueLock:
    @pytest.mark.asyncio
    async def test_extend_succeeds_with_matching_token(self):
        fake = FakeRedis()
        fake.inject("lock:triage:RHEL-123", "tok-1")
        assert await extend_issue_lock(fake, "RHEL-123", "tok-1") is True

    @pytest.mark.asyncio
    async def test_extend_fails_with_wrong_token(self):
        fake = FakeRedis()
        fake.inject("lock:triage:RHEL-123", "tok-1")
        assert await extend_issue_lock(fake, "RHEL-123", "tok-WRONG") is False

    @pytest.mark.asyncio
    async def test_extend_fails_on_expired_key(self):
        fake = FakeRedis()
        assert await extend_issue_lock(fake, "RHEL-123", "tok-1") is False


class TestHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_heartbeat_calls_extend_periodically(self):
        fake = FakeRedis()
        fake.inject("lock:triage:RHEL-123", "tok-1")
        ttl_ms = 150  # short TTL so heartbeat fires quickly (~50ms interval)

        task = asyncio.create_task(_heartbeat_loop(fake, "RHEL-123", "tok-1", ttl_ms))
        try:
            await _wait_until(lambda: len(fake.eval_calls) >= 2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert len(fake.eval_calls) >= 2
        for _, _, args in fake.eval_calls:
            assert args[0] == "lock:triage:RHEL-123"
            assert args[1] == "tok-1"

    @pytest.mark.asyncio
    async def test_heartbeat_stops_when_lock_lost(self):
        fake = FakeRedis()
        # Lock is NOT in the store, so extend will return False immediately
        ttl_ms = 150

        task = asyncio.create_task(_heartbeat_loop(fake, "RHEL-123", "tok-1", ttl_ms))
        # Heartbeat should return on its own (not hang forever)
        await asyncio.wait_for(task, timeout=2.0)

        assert len(fake.eval_calls) == 1

    @pytest.mark.asyncio
    async def test_heartbeat_survives_redis_error(self):
        call_count = 0
        original_eval = FakeRedis.eval

        async def flaky_eval(self, script, numkeys, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("simulated redis error")
            return await original_eval(self, script, numkeys, *args)

        fake = FakeRedis()
        fake.inject("lock:triage:RHEL-123", "tok-1")
        fake.eval = flaky_eval.__get__(fake)
        ttl_ms = 150

        task = asyncio.create_task(_heartbeat_loop(fake, "RHEL-123", "tok-1", ttl_ms))
        try:
            # Should survive the first error and succeed on the second call
            await _wait_until(lambda: len(fake.eval_calls) >= 1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_heartbeat_is_cleanly_cancellable(self):
        fake = FakeRedis()
        fake.inject("lock:triage:RHEL-123", "tok-1")

        task = asyncio.create_task(_heartbeat_loop(fake, "RHEL-123", "tok-1", 60_000))
        await asyncio.sleep(0.01)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestIssueLockContextManager:
    @pytest.mark.asyncio
    async def test_acquire_and_release_on_normal_exit(self):
        fake = FakeRedis()

        async with issue_lock(fake, "RHEL-123", ttl_ms=300) as token:
            assert fake._store["lock:triage:RHEL-123"] == token

        assert "lock:triage:RHEL-123" not in fake._store

    @pytest.mark.asyncio
    async def test_release_on_exception(self):
        fake = FakeRedis()

        with pytest.raises(ValueError):
            async with issue_lock(fake, "RHEL-123", ttl_ms=300):
                raise ValueError("boom")

        assert "lock:triage:RHEL-123" not in fake._store

    @pytest.mark.asyncio
    async def test_release_on_cancellation(self):
        fake = FakeRedis()
        entered = asyncio.Event()

        async def hold_lock():
            async with issue_lock(fake, "RHEL-123", ttl_ms=300):
                entered.set()
                await asyncio.Event().wait()  # hang until cancelled

        task = asyncio.create_task(hold_lock())
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert "lock:triage:RHEL-123" in fake._store

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "lock:triage:RHEL-123" not in fake._store

    @pytest.mark.asyncio
    async def test_yields_none_when_lock_already_held(self):
        fake = FakeRedis()
        fake.inject("lock:triage:RHEL-123", "other-holder")

        async with issue_lock(fake, "RHEL-123", ttl_ms=300) as token:
            assert token is None

        assert fake._store["lock:triage:RHEL-123"] == "other-holder"

    @pytest.mark.asyncio
    async def test_two_concurrent_locks_on_same_issue(self):
        fake = FakeRedis()
        winner_entered = asyncio.Event()

        async def winner():
            async with issue_lock(fake, "RHEL-123", ttl_ms=300):
                winner_entered.set()
                await asyncio.sleep(0.1)

        async def loser():
            await winner_entered.wait()
            async with issue_lock(fake, "RHEL-123", ttl_ms=300) as token:
                assert token is None

        await asyncio.gather(
            asyncio.create_task(winner()),
            asyncio.create_task(loser()),
        )

    @pytest.mark.asyncio
    async def test_different_issues_do_not_conflict(self):
        fake = FakeRedis()

        async with (
            issue_lock(fake, "RHEL-111", ttl_ms=300) as tok1,
            issue_lock(fake, "RHEL-222", ttl_ms=300) as tok2,
        ):
            assert tok1 != tok2
            assert "lock:triage:RHEL-111" in fake._store
            assert "lock:triage:RHEL-222" in fake._store

        assert "lock:triage:RHEL-111" not in fake._store
        assert "lock:triage:RHEL-222" not in fake._store

    @pytest.mark.asyncio
    async def test_custom_prefix(self):
        fake = FakeRedis()

        async with issue_lock(fake, "RHEL-123", ttl_ms=300, prefix="lock:rebase:") as token:
            assert token is not None
            assert "lock:rebase:RHEL-123" in fake._store
            assert "lock:triage:RHEL-123" not in fake._store

        assert "lock:rebase:RHEL-123" not in fake._store

    @pytest.mark.asyncio
    async def test_different_prefixes_do_not_conflict(self):
        fake = FakeRedis()

        async with (
            issue_lock(fake, "RHEL-123", ttl_ms=300, prefix="lock:triage:") as tok1,
            issue_lock(fake, "RHEL-123", ttl_ms=300, prefix="lock:rebase:") as tok2,
        ):
            assert tok1 is not None
            assert tok2 is not None
            assert tok1 != tok2
