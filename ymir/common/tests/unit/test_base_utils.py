import asyncio
import signal

import pytest
from flexmock import flexmock

from ymir.common.base_utils import _race_shutdown, install_shutdown_handler, run_task_loop

_NO_DELAYED_RESULT = object()


class FakeRedis:
    """Minimal stand-in for the pieces of redis.asyncio.Redis that run_task_loop uses.

    `brpop_results` is consumed in order for successive calls; once exhausted,
    brpop resolves to None after a short simulated delay (mirroring a real
    BRPOP timing out with no data available - it's always eventually
    resolved, just possibly after `poll_timeout`), unless a delayed result is
    armed via `arm_delayed_result`.
    """

    def __init__(self, brpop_results: list[tuple[bytes, bytes] | None] | None = None):
        self._results = list(brpop_results or [])
        self.brpop_calls = 0
        self.rpush_calls: list[tuple[bytes, bytes]] = []
        self._delayed_result: tuple[bytes, bytes] | None | object = _NO_DELAYED_RESULT
        self._release_delayed = asyncio.Event()
        self._rpush_failures: set[tuple[bytes, bytes]] = set()

    async def brpop(self, queues, timeout=None):
        self.brpop_calls += 1
        if self._results:
            return self._results.pop(0)
        if self._delayed_result is not _NO_DELAYED_RESULT:
            await self._release_delayed.wait()
            return self._delayed_result
        await asyncio.sleep(0.05)
        return None

    def arm_delayed_result(self, result: tuple[bytes, bytes] | None) -> None:
        """Make the next (post-exhaustion) brpop call block until
        `release_delayed()` is called, then return `result` — simulating a
        BRPOP that was already in flight when shutdown fired and only
        resolves (with a real item, or a natural timeout) afterwards."""
        self._delayed_result = result

    def release_delayed(self) -> None:
        self._release_delayed.set()

    def fail_rpush_for(self, queue: bytes, payload: bytes) -> None:
        self._rpush_failures.add((queue, payload))

    async def rpush(self, queue, payload):
        if (queue, payload) in self._rpush_failures:
            raise RuntimeError("simulated rpush failure")
        self.rpush_calls.append((queue, payload))
        return 1


async def _wait_until(predicate, timeout=2.0, interval=0.005):
    async def _poll():
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def _hang_forever(_payload: bytes) -> None:
    await asyncio.Event().wait()


async def _noop(_payload: bytes) -> None:
    return None


class TestRunTaskLoopShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_already_set_before_start_does_nothing(self):
        """If shutdown is requested before the loop ever starts, it exits
        immediately without polling or re-pushing anything."""
        fake_redis = FakeRedis([(b"queue1", b"payload1")])
        shutdown_event = asyncio.Event()
        shutdown_event.set()

        await asyncio.wait_for(
            run_task_loop(fake_redis, ["queue1"], _noop, shutdown_event=shutdown_event),
            timeout=1,
        )

        assert fake_redis.brpop_calls == 0
        assert fake_redis.rpush_calls == []

    @pytest.mark.asyncio
    async def test_active_task_is_cancelled_and_repushed_on_shutdown(self):
        """This is also a regression test for a deadlock: with
        max_concurrent=1 and the only slot held by a task that never
        finishes on its own (e.g. hours-long build polling), the loop must
        still notice shutdown and unblock rather than wait forever on
        sem.acquire() for a slot that will never free up naturally."""
        fake_redis = FakeRedis([(b"queue1", b"payload1")])
        shutdown_event = asyncio.Event()

        loop_task = asyncio.create_task(
            run_task_loop(
                fake_redis,
                ["queue1"],
                _hang_forever,
                max_concurrent=1,
                shutdown_event=shutdown_event,
            )
        )
        await _wait_until(lambda: fake_redis.brpop_calls >= 1)
        # Give the popped task a moment to actually start running.
        await asyncio.sleep(0.01)

        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=1)

        assert fake_redis.rpush_calls == [(b"queue1", b"payload1")]

    @pytest.mark.asyncio
    async def test_completed_task_is_not_repushed(self):
        """A task that finishes on its own before shutdown must not be
        re-pushed — only genuinely still-running work should be."""
        fake_redis = FakeRedis([(b"queue1", b"payload1")])
        shutdown_event = asyncio.Event()

        loop_task = asyncio.create_task(
            run_task_loop(
                fake_redis,
                ["queue1"],
                _noop,
                max_concurrent=1,
                shutdown_event=shutdown_event,
            )
        )
        # First item is consumed and processed instantly; wait for the loop
        # to come back around and start (hanging) on the next poll before
        # triggering shutdown, so there's no active task left by then.
        await _wait_until(lambda: fake_redis.brpop_calls >= 2)

        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=1)

        assert fake_redis.rpush_calls == []

    @pytest.mark.asyncio
    async def test_no_new_tasks_pulled_after_shutdown(self):
        """Once shutdown fires, the loop must not keep pulling additional
        tasks off the queue even if more are available."""
        fake_redis = FakeRedis([(b"queue1", b"payload1"), (b"queue1", b"payload2")])
        shutdown_event = asyncio.Event()

        loop_task = asyncio.create_task(
            run_task_loop(
                fake_redis,
                ["queue1"],
                _hang_forever,
                max_concurrent=1,
                shutdown_event=shutdown_event,
            )
        )
        await _wait_until(lambda: fake_redis.brpop_calls >= 1)
        await asyncio.sleep(0.01)

        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=1)

        # Only the first item was ever popped; the second is untouched in
        # the "queue" and the first was re-pushed once, unprocessed.
        assert fake_redis.brpop_calls == 1
        assert fake_redis.rpush_calls == [(b"queue1", b"payload1")]

    @pytest.mark.asyncio
    async def test_orphaned_poll_result_is_repushed_after_shutdown(self):
        """Regression test: if shutdown fires while a BRPOP is already in
        flight (so it's abandoned rather than cancelled, per _race_shutdown's
        docstring), and that BRPOP later resolves with a real task rather
        than a natural timeout, the task must not be silently consumed from
        Redis and dropped — it must be re-pushed."""
        fake_redis = FakeRedis()
        fake_redis.arm_delayed_result((b"queue1", b"payload1"))
        shutdown_event = asyncio.Event()

        loop_task = asyncio.create_task(
            run_task_loop(
                fake_redis,
                ["queue1"],
                _noop,
                max_concurrent=1,
                shutdown_event=shutdown_event,
            )
        )
        await _wait_until(lambda: fake_redis.brpop_calls >= 1)
        await asyncio.sleep(0.01)

        shutdown_event.set()
        # The loop must not exit until the in-flight BRPOP is resolved one
        # way or another - it can't just abandon it and return.
        await asyncio.sleep(0.05)
        assert not loop_task.done()

        fake_redis.release_delayed()
        await asyncio.wait_for(loop_task, timeout=1)

        assert fake_redis.rpush_calls == [(b"queue1", b"payload1")]

    @pytest.mark.asyncio
    async def test_orphaned_poll_natural_timeout_repushes_nothing(self):
        """If the in-flight BRPOP that was abandoned on shutdown resolves
        with None (a natural timeout, no data arrived), there's nothing to
        re-push."""
        fake_redis = FakeRedis()
        fake_redis.arm_delayed_result(None)
        shutdown_event = asyncio.Event()

        loop_task = asyncio.create_task(
            run_task_loop(
                fake_redis,
                ["queue1"],
                _noop,
                max_concurrent=1,
                shutdown_event=shutdown_event,
            )
        )
        await _wait_until(lambda: fake_redis.brpop_calls >= 1)
        await asyncio.sleep(0.01)

        shutdown_event.set()
        fake_redis.release_delayed()
        await asyncio.wait_for(loop_task, timeout=1)

        assert fake_redis.rpush_calls == []

    @pytest.mark.asyncio
    async def test_repush_failure_does_not_abort_remaining_tasks(self):
        """If RPUSH fails for one task during shutdown, the rest must still
        be re-pushed rather than the whole batch being aborted."""
        fake_redis = FakeRedis([(b"q1", b"p1"), (b"q1", b"p2")])
        fake_redis.fail_rpush_for(b"q1", b"p1")
        shutdown_event = asyncio.Event()

        loop_task = asyncio.create_task(
            run_task_loop(
                fake_redis,
                ["q1"],
                _hang_forever,
                max_concurrent=2,
                shutdown_event=shutdown_event,
            )
        )
        await _wait_until(lambda: fake_redis.brpop_calls >= 2)
        await asyncio.sleep(0.01)

        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=1)

        assert fake_redis.rpush_calls == [(b"q1", b"p2")]

    @pytest.mark.asyncio
    async def test_orphan_poll_rpush_failure_does_not_crash(self):
        """If the RPUSH for an orphaned poll result fails, run_task_loop
        must return cleanly instead of raising."""
        fake_redis = FakeRedis()
        fake_redis.arm_delayed_result((b"q1", b"p1"))
        fake_redis.fail_rpush_for(b"q1", b"p1")
        shutdown_event = asyncio.Event()

        loop_task = asyncio.create_task(
            run_task_loop(
                fake_redis,
                ["q1"],
                _noop,
                max_concurrent=1,
                shutdown_event=shutdown_event,
            )
        )
        await _wait_until(lambda: fake_redis.brpop_calls >= 1)
        await asyncio.sleep(0.01)

        shutdown_event.set()
        fake_redis.release_delayed()
        await asyncio.wait_for(loop_task, timeout=1)

        assert fake_redis.rpush_calls == []

    @pytest.mark.asyncio
    async def test_custom_poll_fn_idle_sleep_is_interrupted_by_shutdown(self):
        """A custom poll_fn (e.g. mr_consolidation_agent) that repeatedly
        returns None while idle must not delay shutdown by the full
        poll_timeout."""
        fake_redis = FakeRedis()
        shutdown_event = asyncio.Event()

        async def poll_fn():
            return None

        loop_task = asyncio.create_task(
            run_task_loop(
                fake_redis,
                [],
                _noop,
                poll_fn=poll_fn,
                poll_timeout=100,
                shutdown_event=shutdown_event,
            )
        )
        await asyncio.sleep(0.05)
        shutdown_event.set()

        # Would take up to 100s if the idle-sleep weren't interruptible.
        await asyncio.wait_for(loop_task, timeout=1)

    @pytest.mark.asyncio
    async def test_max_concurrent_below_one_raises(self):
        with pytest.raises(ValueError, match="max_concurrent must be at least 1"):
            await run_task_loop(FakeRedis(), ["queue1"], _noop, max_concurrent=0)


class TestRaceShutdown:
    @pytest.mark.asyncio
    async def test_outer_cancellation_cleans_up_both_tasks(self):
        """If the caller of _race_shutdown is itself cancelled, the two
        internal tasks must not be left running in the background."""
        shutdown_event = asyncio.Event()
        started = asyncio.Event()

        async def slow_coro():
            started.set()
            await asyncio.Event().wait()

        runner = asyncio.ensure_future(_race_shutdown(slow_coro(), shutdown_event, cancel_on_shutdown=True))
        await asyncio.wait_for(started.wait(), timeout=1)

        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        await asyncio.sleep(0)
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert leftover == []


class TestInstallShutdownHandler:
    def test_registers_sigterm_and_sigint(self):
        shutdown_event = asyncio.Event()
        registered: list[tuple[int, object]] = []

        fake_loop = flexmock()
        fake_loop.should_receive("add_signal_handler").replace_with(
            lambda sig, callback: registered.append((sig, callback))
        )

        install_shutdown_handler(fake_loop, shutdown_event)

        assert {sig for sig, _ in registered} == {signal.SIGTERM, signal.SIGINT}
        assert all(callback == shutdown_event.set for _, callback in registered)
