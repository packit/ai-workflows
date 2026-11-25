"""Unit tests for ymir.sweep.base (SweepStrategy orchestration)."""

import pytest
from flexmock import flexmock

from ymir.common.constants import JiraLabels
from ymir.common.models import FullIssue
from ymir.sweep.base import SweepResult, SweepStrategy
from ymir.sweep.comment_parser import CommentData
from ymir.sweep.tests.unit.conftest import make_issue, make_ymir_comment

# ---------------------------------------------------------------------------
# Concrete strategy for testing the base class
# ---------------------------------------------------------------------------


class _UnblockedStrategy(SweepStrategy):
    """Always reports every issue as unblocked."""

    name = "test_unblocked"
    label = JiraLabels.YMIR_POSTPONED_DEPENDENCY

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        return SweepResult(issue_key=issue.key, action="unblocked", detail="Fixed!")


class _StillBlockedStrategy(SweepStrategy):
    name = "test_still_blocked"
    label = JiraLabels.YMIR_POSTPONED_DEPENDENCY

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        return SweepResult(issue_key=issue.key, action="still_blocked", detail="Not yet")


class _ErrorStrategy(SweepStrategy):
    name = "test_error"
    label = JiraLabels.YMIR_POSTPONED_DEPENDENCY

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        return SweepResult(issue_key=issue.key, action="error", detail="Something failed")


class _TransitionedStrategy(SweepStrategy):
    name = "test_transitioned"
    label = JiraLabels.YMIR_POSTPONED_DEPENDENCY

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        return SweepResult(issue_key=issue.key, action="transitioned", detail="Category changed")


class _RaisingStrategy(SweepStrategy):
    """Raises an unexpected exception from is_unblocked."""

    name = "test_raising"
    label = JiraLabels.YMIR_POSTPONED_DEPENDENCY

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        raise RuntimeError("Unexpected error")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_remove_label(monkeypatch):
    monkeypatch.setattr("ymir.sweep.base.remove_issue_label", lambda *a, **kw: None)


def _mock_add_label(monkeypatch):
    monkeypatch.setattr("ymir.sweep.base.add_issue_label", lambda *a, **kw: None)


def _issue_with_comment(**kw):
    comment = make_ymir_comment(
        pending_issues=["RHEL-99"],
        blocker_reference="RHEL-99",
        **kw,
    )
    return make_issue(comments=[comment])


# ---------------------------------------------------------------------------
# Tests: get_blocked_issues
# ---------------------------------------------------------------------------


def test_get_blocked_issues_uses_correct_jql(monkeypatch):
    captured = {}

    def mock_get_current_issues(jql, full=False):
        captured["jql"] = jql
        return iter([])

    monkeypatch.setattr("ymir.sweep.base.get_current_issues", mock_get_current_issues)

    strategy = _UnblockedStrategy()
    strategy.get_blocked_issues()

    assert (
        captured["jql"] == f'labels = "{JiraLabels.YMIR_POSTPONED_DEPENDENCY.value}" AND status != "Closed"'
    )


# ---------------------------------------------------------------------------
# Tests: on_unblock
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: on_transition
# ---------------------------------------------------------------------------


def test_on_transition_adds_new_label_before_removing_old(monkeypatch):
    call_order = []

    monkeypatch.setattr(
        "ymir.sweep.base.add_issue_label",
        lambda *a, **kw: call_order.append(("add", a[1])),
    )
    monkeypatch.setattr(
        "ymir.sweep.base.remove_issue_label",
        lambda *a, **kw: call_order.append(("remove", a[1])),
    )

    strategy = _UnblockedStrategy()
    strategy.on_transition(
        "RHEL-12345",
        JiraLabels.YMIR_POSTPONED_NO_PATCH,
        comment="Switching category.",
    )

    assert call_order[0] == ("add", JiraLabels.YMIR_POSTPONED_NO_PATCH.value)
    assert call_order[1] == ("remove", JiraLabels.YMIR_POSTPONED_DEPENDENCY.value)


# ---------------------------------------------------------------------------
# Tests: run()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_counts_unblocked(monkeypatch):
    issue = _issue_with_comment()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))

    async def submit(_client, intent):
        assert intent.retry_label == JiraLabels.YMIR_POSTPONED_DEPENDENCY.value

    monkeypatch.setattr("ymir.sweep.base.submit_triage_intent", submit)
    client = flexmock()

    strategy = _UnblockedStrategy()
    summary = await strategy.run(client)

    assert summary["total"] == 1
    assert summary["unblocked"] == 1
    assert summary["errors"] == 0
    assert summary["still_blocked"] == 0


@pytest.mark.asyncio
async def test_run_counts_still_blocked(monkeypatch):
    issue = _issue_with_comment()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))

    strategy = _StillBlockedStrategy()
    summary = await strategy.run(flexmock().should_receive("lpush").never().mock())

    assert summary["still_blocked"] == 1
    assert summary["unblocked"] == 0


@pytest.mark.asyncio
async def test_run_counts_errors(monkeypatch):
    issue = _issue_with_comment()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))

    strategy = _ErrorStrategy()
    summary = await strategy.run(flexmock())

    assert summary["errors"] == 1
    assert summary["unblocked"] == 0


@pytest.mark.asyncio
async def test_run_skips_issues_with_no_ymir_comment(monkeypatch):
    issue = make_issue(comments=[])  # no Ymir comment
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))

    strategy = _UnblockedStrategy()
    summary = await strategy.run(flexmock())

    assert summary["errors"] == 1
    assert summary["unblocked"] == 0


@pytest.mark.asyncio
async def test_run_catches_unexpected_exceptions(monkeypatch):
    issue = _issue_with_comment()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))

    strategy = _RaisingStrategy()
    summary = await strategy.run(flexmock())

    assert summary["errors"] == 1


@pytest.mark.asyncio
async def test_run_counts_transitioned(monkeypatch):
    issue = _issue_with_comment()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))

    strategy = _TransitionedStrategy()
    summary = await strategy.run(flexmock())

    assert summary["transitioned"] == 1
    assert summary["unblocked"] == 0
    assert summary["errors"] == 0
    assert summary["still_blocked"] == 0


@pytest.mark.asyncio
async def test_run_processes_multiple_issues(monkeypatch):
    issues = [_issue_with_comment(summary=f"reason {i}") for i in range(3)]
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter(issues))

    async def submit(_client, intent):
        assert intent.retry_label == JiraLabels.YMIR_POSTPONED_DEPENDENCY.value

    monkeypatch.setattr("ymir.sweep.base.submit_triage_intent", submit)
    client = flexmock()

    strategy = _UnblockedStrategy()
    summary = await strategy.run(client)

    assert summary["total"] == 3
    assert summary["unblocked"] == 3
