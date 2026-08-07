"""Unit tests for ymir.sweep.no_patch.NoPatchSweep."""

import pytest
import redis
from flexmock import flexmock

from ymir.common.constants import JiraLabels, RedisQueues
from ymir.sweep.no_patch import _MAX_ISSUES_DEFAULT, NoPatchSweep
from ymir.sweep.tests.unit.conftest import make_issue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis():
    r = flexmock()
    r.should_receive("lpush").and_return(1)
    return r


def _patch_remove_label(monkeypatch):
    monkeypatch.setattr("ymir.sweep.no_patch.remove_issue_label", lambda *a, **kw: None)


def _patch_add_label(monkeypatch):
    monkeypatch.setattr("ymir.sweep.no_patch.add_issue_label", lambda *a, **kw: None)


def _set_get_blocked(monkeypatch, issues):
    monkeypatch.setattr(
        "ymir.sweep.base.get_current_issues",
        lambda jql, full=False: iter(issues),
    )


# ---------------------------------------------------------------------------
# NoPatchSweep.run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_requeues_eligible_issues(monkeypatch):
    issues = [make_issue(key=f"RHEL-{i}") for i in range(3)]
    _set_get_blocked(monkeypatch, issues)
    _patch_remove_label(monkeypatch)
    _patch_add_label(monkeypatch)

    queued = []
    mock_redis = flexmock()
    mock_redis.should_receive("lpush").replace_with(lambda queue, payload: queued.append(queue))

    summary = await NoPatchSweep().run(mock_redis)

    assert summary["total"] == 3
    assert summary["unblocked"] == 3
    assert summary["still_blocked"] == 0
    assert summary["errors"] == 0
    assert all(q == RedisQueues.TRIAGE_QUEUE.value for q in queued)


@pytest.mark.asyncio
async def test_run_skips_in_progress_issues(monkeypatch):
    in_progress = make_issue(key="RHEL-1", labels=[JiraLabels.TRIAGE_IN_PROGRESS.value])
    eligible = make_issue(key="RHEL-2", labels=[])
    _set_get_blocked(monkeypatch, [in_progress, eligible])
    _patch_remove_label(monkeypatch)
    _patch_add_label(monkeypatch)
    mock_redis = _make_redis()

    summary = await NoPatchSweep().run(mock_redis)

    assert summary["total"] == 2
    assert summary["unblocked"] == 1
    assert summary["still_blocked"] == 1  # the in-progress issue


@pytest.mark.asyncio
async def test_run_respects_max_issues_per_run_env(monkeypatch):
    monkeypatch.setenv("MAX_ISSUES_PER_RUN", "2")
    issues = [make_issue(key=f"RHEL-{i}") for i in range(5)]
    _set_get_blocked(monkeypatch, issues)
    _patch_remove_label(monkeypatch)
    _patch_add_label(monkeypatch)
    mock_redis = _make_redis()

    summary = await NoPatchSweep().run(mock_redis)

    assert summary["unblocked"] == 2
    # 3 issues capped for next run, counted as still_blocked
    assert summary["still_blocked"] == 3


@pytest.mark.asyncio
async def test_run_uses_default_max_when_env_not_set(monkeypatch):
    monkeypatch.delenv("MAX_ISSUES_PER_RUN", raising=False)
    issues = [make_issue(key=f"RHEL-{i}") for i in range(_MAX_ISSUES_DEFAULT + 5)]
    _set_get_blocked(monkeypatch, issues)
    _patch_remove_label(monkeypatch)
    _patch_add_label(monkeypatch)
    mock_redis = _make_redis()

    summary = await NoPatchSweep().run(mock_redis)

    assert summary["unblocked"] == _MAX_ISSUES_DEFAULT
    assert summary["still_blocked"] == 5


@pytest.mark.asyncio
async def test_run_posts_comment_with_remove_label(monkeypatch):
    issues = [make_issue(key="RHEL-1")]
    _set_get_blocked(monkeypatch, issues)
    _patch_add_label(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        "ymir.sweep.no_patch.remove_issue_label",
        lambda key, label, comment=None: captured.update({"key": key, "label": label, "comment": comment}),
    )
    mock_redis = _make_redis()

    await NoPatchSweep().run(mock_redis)

    assert captured["key"] == "RHEL-1"
    assert captured["label"] == JiraLabels.YMIR_POSTPONED_NO_PATCH.value


@pytest.mark.asyncio
async def test_run_reraises_redis_error(monkeypatch):
    issues = [make_issue(key="RHEL-1")]
    _set_get_blocked(monkeypatch, issues)

    bad_redis = flexmock()
    bad_redis.should_receive("lpush").and_raise(redis.RedisError("connection lost"))

    with pytest.raises(redis.RedisError):
        await NoPatchSweep().run(bad_redis)


@pytest.mark.asyncio
async def test_run_counts_individual_errors(monkeypatch):
    issues = [make_issue(key="RHEL-1"), make_issue(key="RHEL-2")]
    _set_get_blocked(monkeypatch, issues)
    _patch_add_label(monkeypatch)

    call_count = [0]

    def flaky_remove(key, label, comment=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("Jira API blip")

    monkeypatch.setattr("ymir.sweep.no_patch.remove_issue_label", flaky_remove)
    mock_redis = _make_redis()

    summary = await NoPatchSweep().run(mock_redis)

    assert summary["errors"] == 1
    assert summary["unblocked"] == 1


@pytest.mark.asyncio
async def test_run_pushes_to_redis_before_adding_in_progress_label(monkeypatch):
    """Redis push must precede TRIAGE_IN_PROGRESS so a Jira failure leaves
    the issue queued (processed by triage) rather than silently dropped."""
    issues = [make_issue(key="RHEL-1")]
    _set_get_blocked(monkeypatch, issues)

    call_order = []

    mock_redis = flexmock()
    mock_redis.should_receive("lpush").replace_with(lambda queue, payload: call_order.append("redis"))
    monkeypatch.setattr(
        "ymir.sweep.no_patch.add_issue_label",
        lambda *a, **kw: call_order.append("add"),
    )
    monkeypatch.setattr(
        "ymir.sweep.no_patch.remove_issue_label",
        lambda *a, **kw: call_order.append("remove"),
    )

    await NoPatchSweep().run(mock_redis)

    assert call_order == ["redis", "add", "remove"]


@pytest.mark.asyncio
async def test_run_sets_triage_in_progress_before_removing_postponement(monkeypatch):
    """TRIAGE_IN_PROGRESS must be added before the postponement label is removed."""
    issues = [make_issue(key="RHEL-1")]
    _set_get_blocked(monkeypatch, issues)

    label_ops = []
    monkeypatch.setattr(
        "ymir.sweep.no_patch.add_issue_label",
        lambda key, label, **kw: label_ops.append(("add", label)),
    )
    monkeypatch.setattr(
        "ymir.sweep.no_patch.remove_issue_label",
        lambda key, label, **kw: label_ops.append(("remove", label)),
    )
    mock_redis = _make_redis()

    await NoPatchSweep().run(mock_redis)

    add_idx = next(i for i, op in enumerate(label_ops) if op == ("add", JiraLabels.TRIAGE_IN_PROGRESS.value))
    remove_idx = next(i for i, op in enumerate(label_ops) if op[0] == "remove")
    assert add_idx < remove_idx, (
        f"TRIAGE_IN_PROGRESS must be added before postponement removed; got {label_ops}"
    )


@pytest.mark.asyncio
async def test_run_redis_pushed_even_when_label_removal_fails(monkeypatch):
    """If label removal raises after a successful redis push, the issue is in
    the triage queue but still labelled — it will be re-queued on the next
    sweep.  The error count reflects the failure."""
    issues = [make_issue(key="RHEL-1")]
    _set_get_blocked(monkeypatch, issues)
    _patch_add_label(monkeypatch)

    pushed = []
    mock_redis = flexmock()
    mock_redis.should_receive("lpush").replace_with(lambda queue, payload: pushed.append(queue))
    monkeypatch.setattr(
        "ymir.sweep.no_patch.remove_issue_label",
        lambda *a, **kw: (_ for _ in ()).throw(Exception("Jira API failure")),
    )

    summary = await NoPatchSweep().run(mock_redis)

    assert summary["errors"] == 1
    assert len(pushed) == 1  # redis push happened before the failure


@pytest.mark.asyncio
async def test_run_returns_zero_when_no_issues(monkeypatch):
    _set_get_blocked(monkeypatch, [])
    mock_redis = flexmock()

    summary = await NoPatchSweep().run(mock_redis)

    assert summary == {
        "total": 0,
        "unblocked": 0,
        "transitioned": 0,
        "errors": 0,
        "still_blocked": 0,
    }
