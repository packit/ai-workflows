"""Unit tests for ymir.sweep.base (SweepStrategy orchestration)."""

import pytest
import redis
from flexmock import flexmock

from ymir.common.constants import JiraLabels
from ymir.supervisor.supervisor_types import FullIssue
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


def _mock_redis():
    r = flexmock()
    r.should_receive("lpush").and_return(1)
    return r


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

    assert captured["jql"] == f'labels = "{JiraLabels.YMIR_POSTPONED_DEPENDENCY.value}"'


# ---------------------------------------------------------------------------
# Tests: on_unblock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_unblock_pushes_to_redis_before_adding_label(monkeypatch):
    """Redis push must precede TRIAGE_IN_PROGRESS so a Jira failure leaves the
    issue queued (processed by triage) rather than silently dropped."""
    call_order = []
    issue_key = "RHEL-12345"
    mock_redis = flexmock()
    mock_redis.should_receive("lpush").replace_with(lambda *a: call_order.append("redis"))

    monkeypatch.setattr(
        "ymir.sweep.base.add_issue_label",
        lambda *a, **kw: call_order.append("add"),
    )
    monkeypatch.setattr(
        "ymir.sweep.base.remove_issue_label",
        lambda *a, **kw: call_order.append("remove"),
    )

    strategy = _UnblockedStrategy()
    await strategy.on_unblock(issue_key, mock_redis, comment="Fixed!")

    assert call_order == ["redis", "add", "remove"]


@pytest.mark.asyncio
async def test_on_unblock_sets_triage_in_progress_before_removing_postponement(monkeypatch):
    """TRIAGE_IN_PROGRESS must be added before the postponement label is removed
    so there is no window where the issue has no Ymir tracking label."""
    call_order = []

    mock_redis = flexmock()
    mock_redis.should_receive("lpush").and_return(1)

    monkeypatch.setattr(
        "ymir.sweep.base.add_issue_label",
        lambda key, label, **kw: call_order.append(("add", label)),
    )
    monkeypatch.setattr(
        "ymir.sweep.base.remove_issue_label",
        lambda key, label, **kw: call_order.append(("remove", label)),
    )

    strategy = _UnblockedStrategy()
    await strategy.on_unblock("RHEL-12345", mock_redis)

    add_idx = next(i for i, op in enumerate(call_order) if op == ("add", JiraLabels.TRIAGE_IN_PROGRESS.value))
    remove_idx = next(i for i, op in enumerate(call_order) if op[0] == "remove")
    assert add_idx < remove_idx, (
        f"TRIAGE_IN_PROGRESS must be added before postponement removed; got {call_order}"
    )


@pytest.mark.asyncio
async def test_on_unblock_passes_comment_to_remove_label(monkeypatch):
    captured = {}

    mock_redis = flexmock()
    mock_redis.should_receive("lpush").and_return(1)

    _mock_add_label(monkeypatch)
    monkeypatch.setattr(
        "ymir.sweep.base.remove_issue_label",
        lambda key, label, comment=None: captured.update({"comment": comment}),
    )

    strategy = _UnblockedStrategy()
    await strategy.on_unblock("RHEL-12345", mock_redis, comment="Resolved.")

    assert captured["comment"] == "Resolved."


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
    _mock_remove_label(monkeypatch)
    _mock_add_label(monkeypatch)
    mock_redis = _mock_redis()

    strategy = _UnblockedStrategy()
    summary = await strategy.run(mock_redis)

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
async def test_run_reraises_redis_error(monkeypatch):
    issue = _issue_with_comment()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr("ymir.sweep.base.remove_issue_label", lambda *a, **kw: None)

    bad_redis = flexmock()
    bad_redis.should_receive("lpush").and_raise(redis.RedisError("connection lost"))

    strategy = _UnblockedStrategy()
    with pytest.raises(redis.RedisError):
        await strategy.run(bad_redis)


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
    _mock_remove_label(monkeypatch)
    _mock_add_label(monkeypatch)
    mock_redis = _mock_redis()

    strategy = _UnblockedStrategy()
    summary = await strategy.run(mock_redis)

    assert summary["total"] == 3
    assert summary["unblocked"] == 3
