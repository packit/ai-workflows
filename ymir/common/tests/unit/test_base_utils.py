import asyncio
import signal

import pytest
from flexmock import flexmock

from ymir.common.base_utils import install_shutdown_handler, run_task_loop


class FakeRedis:
    """Minimal stand-in for the pieces of redis.asyncio.Redis that run_task_loop uses.

    `brpop_results` is consumed in order for successive calls; once exhausted,
    brpop hangs forever (mirroring a real BRPOP with no data available), so
    tests must either not wait on that call or trigger shutdown to abandon it.
    """

    def __init__(self, brpop_results: list[tuple[bytes, bytes] | None] | None = None):
        self._results = list(brpop_results or [])
        self.brpop_calls = 0
        self.rpush_calls: list[tuple[bytes, bytes]] = []

    async def brpop(self, queues, timeout=None):
        self.brpop_calls += 1
        if self._results:
            return self._results.pop(0)
        await asyncio.Event().wait()
        return None

    async def rpush(self, queue, payload):
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
