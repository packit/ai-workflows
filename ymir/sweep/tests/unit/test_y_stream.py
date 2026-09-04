"""Unit tests for ymir.sweep.y_stream.YStreamSweep.

YStreamSweep delegates entirely to ``CheckCveTriageEligibilityTool``; these
tests stub that tool and assert the verdict → SweepResult.action mapping.
"""

from unittest.mock import patch

import pytest

from ymir.common import CVEEligibilityResult, TriageEligibility
from ymir.sweep.comment_parser import CommentData
from ymir.sweep.tests.unit.conftest import make_issue
from ymir.sweep.y_stream import YStreamSweep

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# comment_data is unused by the refactored is_unblocked, but the signature
# still requires it.  A minimal stand-in keeps the call sites readable.
_COMMENT_DATA = CommentData(
    blocker_references=None,
    pending_issues=["RHEL-11111"],
    summary="Y-stream CVE waiting for Z-stream clone",
    comment_id="1",
)


def _elig(eligibility, *, reason="reason", error=None, pending=None, duplicate_of=None):
    """Build the dict shape returned by ``tool.run(...).result``."""
    return CVEEligibilityResult(
        is_cve=True,
        eligibility=eligibility,
        reason=reason,
        error=error,
        pending_zstream_issues=pending,
        duplicate_of=duplicate_of,
    ).model_dump()


def _fake_eligibility_tool(result_dict=None, *, raises=None):
    """Return a fake ``CheckCveTriageEligibilityTool`` class.

    Its ``run()`` either raises ``raises`` or returns an object exposing
    ``.result`` (mirroring ``JSONToolOutput``).
    """

    class _Output:
        result = result_dict

    class _Tool:
        async def run(self, input):
            if raises is not None:
                raise raises
            return _Output()

    return _Tool


def _patch_tool(monkeypatch, **kwargs):
    monkeypatch.setattr(
        "ymir.sweep.y_stream.CheckCveTriageEligibilityTool",
        _fake_eligibility_tool(**kwargs),
    )


# ---------------------------------------------------------------------------
# YStreamSweep.is_unblocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_dependencies_still_blocked(monkeypatch):
    _patch_tool(
        monkeypatch,
        result_dict=_elig(
            TriageEligibility.PENDING_DEPENDENCIES,
            reason="waiting for Z-stream clone to ship",
            pending=["RHEL-11111"],
        ),
    )

    result = await YStreamSweep().is_unblocked(make_issue(), _COMMENT_DATA)

    assert result.action == "still_blocked"
    assert result.detail == "waiting for Z-stream clone to ship"


@pytest.mark.asyncio
async def test_immediately_unblocked(monkeypatch):
    _patch_tool(
        monkeypatch,
        result_dict=_elig(TriageEligibility.IMMEDIATELY, reason="at least one clone shipped"),
    )

    result = await YStreamSweep().is_unblocked(make_issue(), _COMMENT_DATA)

    assert result.action == "unblocked"
    assert "immediately" in result.detail


@pytest.mark.asyncio
async def test_never_without_error_unblocked(monkeypatch):
    """The behavioural heart of the refactor: a terminal NEVER verdict must
    leave the sweep population (unblock + re-triage), not stay postponed."""
    _patch_tool(
        monkeypatch,
        result_dict=_elig(TriageEligibility.NEVER, reason="CentOS Stream first — fix inherited"),
    )

    result = await YStreamSweep().is_unblocked(make_issue(), _COMMENT_DATA)

    assert result.action == "unblocked"
    assert "never" in result.detail


@pytest.mark.asyncio
async def test_never_with_error_is_error(monkeypatch):
    """Guards the ordering bug: a transient failure rides on NEVER + error and
    must keep the issue postponed, not un-postpone it."""
    _patch_tool(
        monkeypatch,
        result_dict=_elig(
            TriageEligibility.NEVER,
            reason="clone dependency check failed",
            error="clone dependency check failed: timeout",
        ),
    )

    result = await YStreamSweep().is_unblocked(make_issue(), _COMMENT_DATA)

    assert result.action == "error"
    assert "timeout" in result.detail


@pytest.mark.asyncio
async def test_tool_raises_is_error(monkeypatch):
    _patch_tool(monkeypatch, raises=RuntimeError("Jira unreachable"))

    with patch("ymir.sweep.y_stream.sentry_sdk.capture_exception") as mock_capture:
        result = await YStreamSweep().is_unblocked(make_issue(), _COMMENT_DATA)

        assert result.action == "error"
        assert "Jira unreachable" in result.detail
        mock_capture.assert_called_once()
        assert isinstance(mock_capture.call_args[0][0], RuntimeError)


@pytest.mark.asyncio
async def test_never_with_duplicate_unblocked(monkeypatch):
    """A NEVER-due-to-duplicate verdict unblocks; duplicate handling is
    delegated to triage, not reimplemented in the sweep."""
    _patch_tool(
        monkeypatch,
        result_dict=_elig(
            TriageEligibility.NEVER,
            reason="duplicate of RHEL-9",
            duplicate_of="RHEL-9",
        ),
    )

    result = await YStreamSweep().is_unblocked(make_issue(), _COMMENT_DATA)

    assert result.action == "unblocked"
