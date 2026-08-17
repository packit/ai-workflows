import asyncio
import time

import pytest
from beeai_framework.emitter import Emitter
from beeai_framework.errors import AbortError
from beeai_framework.tools import StringToolOutput, ToolRunOptions
from beeai_framework.utils.cancellation import AbortSignal
from pydantic import BaseModel

from ymir.tools.base import CloneableTool, tool_error_context
from ymir.tools.errors import ToolErrorWithContext


class DummyInput(BaseModel):
    value: str = ""


class DummyTool(CloneableTool[DummyInput, ToolRunOptions, StringToolOutput]):
    name = "dummy"
    description = "test tool"
    input_schema = DummyInput

    def _create_emitter(self):
        return Emitter.root().child(namespace=["tool", "test"], creator=self)

    async def _run(self, input, options, context):
        await asyncio.sleep(0.01)
        return StringToolOutput(result="done")


class SlowDummyTool(DummyTool):
    name = "slow_dummy"
    timeout = 0.3

    async def _run(self, input, options, context):
        await asyncio.sleep(10)
        return StringToolOutput(result="should not reach")


class FastDummyTool(DummyTool):
    name = "fast_dummy"
    timeout = 5

    async def _run(self, input, options, context):
        await asyncio.sleep(0.01)
        return StringToolOutput(result="fast")


class NoTimeoutDummyTool(DummyTool):
    name = "no_timeout_dummy"


def test_tool_error_context_wraps_exception():
    with (
        pytest.raises(ToolErrorWithContext, match="Something failed") as exc_info,
        tool_error_context("Something failed", url="https://example.com"),
    ):
        raise RuntimeError("connection refused")

    err = exc_info.value
    assert err.context["additional_context"] == {
        "url": "https://example.com",
        "exception": "RuntimeError: connection refused",
    }


def test_tool_error_context_passes_through_tool_error_with_context():
    with (
        pytest.raises(ToolErrorWithContext, match="specific inner error"),
        tool_error_context("general outer error"),
    ):
        raise ToolErrorWithContext("specific inner error")


def test_tool_error_context_redacts_credentials():
    fake_token = "glpat-0123456789ABCDEFGHIJ"  # pragma: allowlist secret
    with (
        pytest.raises(ToolErrorWithContext) as exc_info,
        tool_error_context("Failed", token=fake_token),
    ):
        raise RuntimeError("401 Unauthorized")

    assert exc_info.value.context["additional_context"]["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_no_timeout_passes_options_through():
    """Tool with timeout=None should not modify options."""
    tool = NoTimeoutDummyTool()
    result = await tool.run({})
    assert result.result == "done"


@pytest.mark.asyncio
async def test_no_timeout_with_caller_options():
    """Tool with timeout=None should pass caller options unchanged."""
    tool = NoTimeoutDummyTool()
    opts = ToolRunOptions()
    result = await tool.run({}, opts)
    assert result.result == "done"


@pytest.mark.asyncio
async def test_timeout_creates_options_when_none():
    """Tool with a timeout should create options with signal when none provided."""
    tool = FastDummyTool()
    result = await tool.run({})
    assert result.result == "fast"


@pytest.mark.asyncio
async def test_timeout_aborts_slow_tool():
    """Tool with a short timeout should abort if _run exceeds the deadline."""
    tool = SlowDummyTool()
    with pytest.raises(AbortError):
        await tool.run({})


@pytest.mark.asyncio
async def test_timeout_with_caller_signal_merges():
    """When both tool timeout and caller signal exist, both should be active."""
    tool = FastDummyTool()
    caller_signal = AbortSignal.timeout(10)
    opts = ToolRunOptions(signal=caller_signal)
    result = await tool.run({}, opts)
    assert result.result == "fast"
    assert not caller_signal.aborted


@pytest.mark.asyncio
async def test_caller_signal_abort_takes_precedence():
    """If the caller signal fires before tool timeout, it should still abort."""

    class SlowRun(FastDummyTool):
        name = "slow_with_caller_abort"
        timeout = 10

        async def _run(self, input, options, context):
            await asyncio.sleep(10)
            return StringToolOutput(result="should not reach")

    duration = 0.3
    tool = SlowRun()
    caller_signal = AbortSignal.timeout(duration=duration)
    opts = ToolRunOptions(signal=caller_signal)
    with pytest.raises(AbortError):
        await tool.run({}, opts)
    assert caller_signal.aborted
    assert str(duration) in caller_signal.reason


@pytest.mark.asyncio
async def test_timeout_fires_within_expected_time():
    """Tool timeout should abort close to the configured deadline, not after _run's full sleep."""
    tool = SlowDummyTool()
    start = time.monotonic()
    with pytest.raises(AbortError):
        await tool.run({})
    elapsed = time.monotonic() - start
    assert tool.timeout
    assert elapsed < tool.timeout + 0.5, f"Abort took {elapsed:.2f}s, expected ~{tool.timeout}s"
    assert elapsed >= tool.timeout - 0.1, f"Abort at {elapsed:.2f}s, before timeout {tool.timeout}s"


@pytest.mark.asyncio
async def test_tool_timeout_wins_over_longer_caller_signal():
    """When the tool timeout is shorter than the caller signal, the tool timeout fires first."""
    tool_timeout = 0.3
    caller_timeout = 10

    class ShortTimeoutTool(DummyTool):
        name = "short_timeout"
        timeout = tool_timeout

        async def _run(self, input, options, context):
            await asyncio.sleep(60)
            return StringToolOutput(result="should not reach")

    tool = ShortTimeoutTool()
    caller_signal = AbortSignal.timeout(caller_timeout)
    opts = ToolRunOptions(signal=caller_signal)

    start = time.monotonic()
    with pytest.raises(AbortError):
        await tool.run({}, opts)
    elapsed = time.monotonic() - start

    assert elapsed < tool_timeout + 0.5, (
        f"Abort took {elapsed:.2f}s, tool timeout {tool_timeout}s should have fired first"
    )
    assert not caller_signal.aborted or elapsed >= caller_timeout, (
        "Caller signal should not have fired before its own deadline"
    )


@pytest.mark.asyncio
async def test_caller_signal_wins_over_longer_tool_timeout():
    """When the caller signal is shorter than the tool timeout, the caller signal fires first."""
    tool_timeout = 10
    caller_timeout = 0.3

    class LongTimeoutTool(DummyTool):
        name = "long_timeout"
        timeout = tool_timeout

        async def _run(self, input, options, context):
            await asyncio.sleep(60)
            return StringToolOutput(result="should not reach")

    tool = LongTimeoutTool()
    caller_signal = AbortSignal.timeout(caller_timeout)
    opts = ToolRunOptions(signal=caller_signal)

    start = time.monotonic()
    with pytest.raises(AbortError):
        await tool.run({}, opts)
    elapsed = time.monotonic() - start

    assert elapsed < caller_timeout + 0.5, (
        f"Abort took {elapsed:.2f}s, caller timeout {caller_timeout}s should have fired first"
    )
    assert caller_signal.aborted
    assert str(caller_timeout) in caller_signal.reason
