"""Unit tests for ymir.sweep.__main__ (CLI entry point and run_sweep)."""

from contextlib import asynccontextmanager

import pytest
from flexmock import flexmock

from ymir.sweep.__main__ import STRATEGIES, run_sweep
from ymir.sweep.dependency import DependencySweep
from ymir.sweep.no_patch import NoPatchSweep
from ymir.sweep.pr_pending import PRPendingSweep
from ymir.sweep.y_stream import YStreamSweep

# ---------------------------------------------------------------------------
# STRATEGIES dict
# ---------------------------------------------------------------------------


def test_strategies_dict_contains_all_expected_keys():
    assert set(STRATEGIES.keys()) == {"dependency", "y_stream", "pr_pending", "no_patch"}


def test_strategies_dict_maps_to_correct_classes():
    assert STRATEGIES["dependency"] is DependencySweep
    assert STRATEGIES["y_stream"] is YStreamSweep
    assert STRATEGIES["pr_pending"] is PRPendingSweep
    assert STRATEGIES["no_patch"] is NoPatchSweep


# ---------------------------------------------------------------------------
# run_sweep
# ---------------------------------------------------------------------------


def _make_mock_strategy(name: str, called: list):
    """Return a strategy class whose run() records its name and returns a
    zero summary, suitable for injection via STRATEGIES."""

    class _MockStrategy:
        def __init__(self):
            pass

        async def run(self, redis_conn):
            called.append(name)
            return {
                "total": 0,
                "unblocked": 0,
                "transitioned": 0,
                "errors": 0,
                "still_blocked": 0,
            }

    return _MockStrategy


def _patch_context_managers(monkeypatch):
    """Replace the async context managers that run_sweep requires so that
    tests don't need a live requests session or Redis connection."""

    @asynccontextmanager
    async def _noop_requests_session():
        yield

    mock_redis = flexmock()

    @asynccontextmanager
    async def _noop_redis_client(url):
        yield mock_redis

    monkeypatch.setattr("ymir.sweep.__main__.with_requests_session", _noop_requests_session)
    monkeypatch.setattr("ymir.sweep.__main__.redis_client", _noop_redis_client)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")

    return mock_redis


@pytest.mark.asyncio
async def test_run_sweep_invokes_single_strategy(monkeypatch):
    called = []
    monkeypatch.setattr(
        "ymir.sweep.__main__.STRATEGIES",
        {"dependency": _make_mock_strategy("dependency", called)},
    )
    _patch_context_managers(monkeypatch)

    await run_sweep(["dependency"])

    assert called == ["dependency"]


@pytest.mark.asyncio
async def test_run_sweep_invokes_all_strategies_in_order(monkeypatch):
    called = []
    monkeypatch.setattr(
        "ymir.sweep.__main__.STRATEGIES",
        {
            "dependency": _make_mock_strategy("dependency", called),
            "y_stream": _make_mock_strategy("y_stream", called),
            "pr_pending": _make_mock_strategy("pr_pending", called),
            "no_patch": _make_mock_strategy("no_patch", called),
        },
    )
    _patch_context_managers(monkeypatch)

    await run_sweep(["dependency", "y_stream", "pr_pending", "no_patch"])

    assert called == ["dependency", "y_stream", "pr_pending", "no_patch"]


@pytest.mark.asyncio
async def test_run_sweep_empty_list_runs_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(
        "ymir.sweep.__main__.STRATEGIES",
        {"dependency": _make_mock_strategy("dependency", called)},
    )
    _patch_context_managers(monkeypatch)

    await run_sweep([])

    assert called == []
