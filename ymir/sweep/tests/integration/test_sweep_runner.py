"""Integration tests for the sweep pipeline.

Each test calls ``strategy.run(redis_conn)`` on a *real* strategy instance
with all external I/O (Jira, GitLab, buildroot) replaced by monkeypatched
stubs.  Unlike the unit tests, which test individual components in isolation,
these tests verify the full cycle:

  issue fetching → comment parsing → unblock/transition check
  → label removal and Redis push

The real ``parse_ymir_comment()`` runs on fixture comments built by
``make_ymir_comment()``, so the parser and the comment format are exercised
together.
"""

import pytest
import requests
from flexmock import flexmock

from ymir.common import CVEEligibilityResult, TriageEligibility
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.supervisor.supervisor_types import Issue, IssueStatus
from ymir.sweep.dependency import DependencySweep
from ymir.sweep.no_patch import NoPatchSweep
from ymir.sweep.pr_pending import PRPendingSweep
from ymir.sweep.tests.integration.conftest import make_redis
from ymir.sweep.tests.unit.conftest import make_issue, make_ymir_comment
from ymir.sweep.y_stream import YStreamSweep

# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def _async_true(*a, **kw) -> bool:
    return True


async def _async_false(*a, **kw) -> bool:
    return False


def _fake_eligibility_tool(eligibility, *, reason="reason", error=None, pending=None):
    """Return a fake ``CheckCveTriageEligibilityTool`` whose ``run()`` yields
    the given eligibility verdict (mirroring ``JSONToolOutput.result``)."""
    result_dict = CVEEligibilityResult(
        is_cve=True,
        eligibility=eligibility,
        reason=reason,
        error=error,
        pending_zstream_issues=pending,
    ).model_dump()

    class _Output:
        result = result_dict

    class _Tool:
        async def run(self, input):
            return _Output()

    return _Tool


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_blocker(
    key: str = "RHEL-67890",
    fixed_in_build: str | None = None,
    components: list[str] | None = None,
) -> Issue:
    return Issue(
        key=key,
        url=f"https://jira.example.com/browse/{key}",
        summary="Blocker issue",
        components=components if components is not None else ["golang"],
        status=IssueStatus.IN_PROGRESS,
        labels=[],
        fix_versions=["rhel-9.7.z"],
        errata_link=None,
        fixed_in_build=fixed_in_build,
    )


def _dependency_issue(blocker_key: str = "RHEL-67890"):
    comment = make_ymir_comment(
        summary="Rebuild of pkg waiting for dep to land in c9s buildroot",
        pending_issues=[blocker_key],
        blocker_reference=blocker_key,
    )
    return make_issue(
        labels=[JiraLabels.YMIR_POSTPONED_DEPENDENCY.value],
        comments=[comment],
    )


def _y_stream_issue(blocker_keys: list[str]):
    comment = make_ymir_comment(
        summary="Y-stream CVE waiting for Z-stream dependencies",
        pending_issues=blocker_keys,
        blocker_reference=blocker_keys[0] if len(blocker_keys) == 1 else None,
    )
    return make_issue(
        labels=[JiraLabels.YMIR_POSTPONED_Y_STREAM.value],
        comments=[comment],
    )


_MR_URL = "https://gitlab.com/redhat/centos-stream/rpms/pkg/-/merge_requests/42"


def _pr_pending_issue(mr_url: str = _MR_URL):
    comment = make_ymir_comment(
        summary="Upstream patch not yet merged",
        pending_issues=["RHEL-12345"],
        blocker_reference=mr_url,
    )
    return make_issue(
        labels=[JiraLabels.YMIR_POSTPONED_PR_PENDING.value],
        comments=[comment],
    )


# ---------------------------------------------------------------------------
# DependencySweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_sweep_unblocks_when_build_in_buildroot(monkeypatch, captured_label_ops):
    issue = _dependency_issue()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr(
        "ymir.sweep.dependency.get_issue",
        lambda key, full=False: _make_blocker(fixed_in_build="golang-1.21.0-1.el9"),
    )
    monkeypatch.setattr("ymir.sweep.dependency.check_build_in_buildroot", _async_true)
    mock_redis, pushed = make_redis()

    summary = await DependencySweep().run(mock_redis)

    assert summary["total"] == 1
    assert summary["unblocked"] == 1
    assert summary["errors"] == 0
    assert summary["still_blocked"] == 0
    assert pushed == [RedisQueues.TRIAGE_QUEUE.value]
    assert (issue.key, JiraLabels.YMIR_POSTPONED_DEPENDENCY.value) in captured_label_ops["remove"]


@pytest.mark.asyncio
async def test_dependency_sweep_still_blocked_when_no_fixed_in_build(monkeypatch, captured_label_ops):
    issue = _dependency_issue()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr(
        "ymir.sweep.dependency.get_issue",
        lambda key, full=False: _make_blocker(fixed_in_build=None),
    )
    mock_redis, pushed = make_redis()

    summary = await DependencySweep().run(mock_redis)

    assert summary["still_blocked"] == 1
    assert summary["unblocked"] == 0
    assert pushed == []
    assert captured_label_ops["remove"] == []


@pytest.mark.asyncio
async def test_dependency_sweep_still_blocked_when_build_not_in_buildroot(monkeypatch, captured_label_ops):
    issue = _dependency_issue()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr(
        "ymir.sweep.dependency.get_issue",
        lambda key, full=False: _make_blocker(fixed_in_build="golang-1.21.0-1.el9"),
    )
    monkeypatch.setattr("ymir.sweep.dependency.check_build_in_buildroot", _async_false)
    mock_redis, pushed = make_redis()

    summary = await DependencySweep().run(mock_redis)

    assert summary["still_blocked"] == 1
    assert summary["unblocked"] == 0
    assert pushed == []


@pytest.mark.asyncio
async def test_dependency_sweep_counts_error_on_blocker_fetch_failure(monkeypatch, captured_label_ops):
    issue = _dependency_issue()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))

    def raise_http(key, full=False):
        resp = flexmock(status_code=404)
        raise requests.HTTPError("Not found", response=resp)

    monkeypatch.setattr("ymir.sweep.dependency.get_issue", raise_http)
    mock_redis, pushed = make_redis()

    summary = await DependencySweep().run(mock_redis)

    assert summary["errors"] == 1
    assert pushed == []


# ---------------------------------------------------------------------------
# YStreamSweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_y_stream_sweep_unblocks_when_no_longer_pending(monkeypatch, captured_label_ops):
    issue = _y_stream_issue(["RHEL-11111", "RHEL-22222"])
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr(
        "ymir.sweep.y_stream.CheckCveTriageEligibilityTool",
        _fake_eligibility_tool(TriageEligibility.IMMEDIATELY, reason="at least one clone shipped"),
    )
    mock_redis, pushed = make_redis()

    summary = await YStreamSweep().run(mock_redis)

    assert summary["unblocked"] == 1
    assert pushed == [RedisQueues.TRIAGE_QUEUE.value]
    assert (issue.key, JiraLabels.YMIR_POSTPONED_Y_STREAM.value) in captured_label_ops["remove"]


@pytest.mark.asyncio
async def test_y_stream_sweep_still_blocked_when_pending(monkeypatch, captured_label_ops):
    issue = _y_stream_issue(["RHEL-11111", "RHEL-22222"])
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr(
        "ymir.sweep.y_stream.CheckCveTriageEligibilityTool",
        _fake_eligibility_tool(
            TriageEligibility.PENDING_DEPENDENCIES,
            reason="waiting for Z-stream clone to ship",
            pending=["RHEL-22222"],
        ),
    )
    mock_redis, pushed = make_redis()

    summary = await YStreamSweep().run(mock_redis)

    assert summary["still_blocked"] == 1
    assert summary["unblocked"] == 0
    assert pushed == []


# ---------------------------------------------------------------------------
# PRPendingSweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_pending_sweep_unblocks_when_mr_merged(monkeypatch, captured_label_ops):
    issue = _pr_pending_issue()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr(
        "ymir.sweep.pr_pending.gitlab_api_get",
        lambda path, *, gitlab_url=None, params=None: {"state": "merged"},
    )
    mock_redis, pushed = make_redis()

    summary = await PRPendingSweep().run(mock_redis)

    assert summary["unblocked"] == 1
    assert pushed == [RedisQueues.TRIAGE_QUEUE.value]
    assert (issue.key, JiraLabels.YMIR_POSTPONED_PR_PENDING.value) in captured_label_ops["remove"]


@pytest.mark.asyncio
async def test_pr_pending_sweep_transitions_to_no_patch_when_mr_closed(monkeypatch, captured_label_ops):
    issue = _pr_pending_issue()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr(
        "ymir.sweep.pr_pending.gitlab_api_get",
        lambda path, *, gitlab_url=None, params=None: {"state": "closed"},
    )
    mock_redis, pushed = make_redis()

    summary = await PRPendingSweep().run(mock_redis)

    assert summary["transitioned"] == 1
    assert pushed == []
    # New label is added before old label is removed (safety invariant from base.on_transition)
    assert captured_label_ops["add"][0] == (issue.key, JiraLabels.YMIR_POSTPONED_NO_PATCH.value)
    assert (issue.key, JiraLabels.YMIR_POSTPONED_PR_PENDING.value) in captured_label_ops["remove"]


@pytest.mark.asyncio
async def test_pr_pending_sweep_still_blocked_when_mr_open(monkeypatch, captured_label_ops):
    issue = _pr_pending_issue()
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter([issue]))
    monkeypatch.setattr(
        "ymir.sweep.pr_pending.gitlab_api_get",
        lambda path, *, gitlab_url=None, params=None: {"state": "opened"},
    )
    mock_redis, pushed = make_redis()

    summary = await PRPendingSweep().run(mock_redis)

    assert summary["still_blocked"] == 1
    assert pushed == []
    assert captured_label_ops["remove"] == []


# ---------------------------------------------------------------------------
# NoPatchSweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_patch_sweep_requeues_up_to_cap(monkeypatch, captured_label_ops):
    monkeypatch.setenv("MAX_ISSUES_PER_RUN", "2")
    issues = [
        make_issue(key=f"RHEL-{i}", labels=[JiraLabels.YMIR_POSTPONED_NO_PATCH.value]) for i in range(3)
    ]
    monkeypatch.setattr("ymir.sweep.base.get_current_issues", lambda jql, full=False: iter(issues))
    mock_redis, pushed = make_redis()

    summary = await NoPatchSweep().run(mock_redis)

    assert summary["total"] == 3
    assert summary["unblocked"] == 2
    assert summary["still_blocked"] == 1
    assert len(pushed) == 2
    assert all(q == RedisQueues.TRIAGE_QUEUE.value for q in pushed)
    assert len(captured_label_ops["remove"]) == 2


@pytest.mark.asyncio
async def test_no_patch_sweep_skips_in_progress_issues(monkeypatch, captured_label_ops):
    in_progress = make_issue(
        key="RHEL-100",
        labels=[JiraLabels.TRIAGE_IN_PROGRESS.value, JiraLabels.YMIR_POSTPONED_NO_PATCH.value],
    )
    eligible = make_issue(key="RHEL-101", labels=[JiraLabels.YMIR_POSTPONED_NO_PATCH.value])
    monkeypatch.setattr(
        "ymir.sweep.base.get_current_issues",
        lambda jql, full=False: iter([in_progress, eligible]),
    )
    mock_redis, pushed = make_redis()

    summary = await NoPatchSweep().run(mock_redis)

    assert summary["total"] == 2
    assert summary["unblocked"] == 1
    assert summary["still_blocked"] == 1
    assert len(pushed) == 1
    removed_keys = [key for key, _ in captured_label_ops["remove"]]
    assert "RHEL-100" not in removed_keys
    assert "RHEL-101" in removed_keys
