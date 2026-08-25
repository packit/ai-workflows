"""Unit tests for ymir.sweep.dependency.DependencySweep."""

import pytest
import requests
from flexmock import flexmock

from ymir.supervisor.supervisor_types import IssueStatus
from ymir.sweep.dependency import DependencySweep, _resolve_target_branch, branch_from_fix_versions
from ymir.sweep.tests.unit.conftest import make_issue, make_ymir_comment

# ---------------------------------------------------------------------------
# _branch_from_fix_versions
# ---------------------------------------------------------------------------


def test_branch_from_fix_versions_rhel9():
    assert branch_from_fix_versions(["rhel-9.6.0"]) == "c9s"


def test_branch_from_fix_versions_rhel10():
    assert branch_from_fix_versions(["rhel-10.1"]) == "c10s"


def test_branch_from_fix_versions_rhel8():
    assert branch_from_fix_versions(["rhel-8.10"]) == "c8s"


def test_branch_from_fix_versions_z_stream():
    assert branch_from_fix_versions(["rhel-9.7.z"]) == "c9s"


def test_branch_from_fix_versions_empty():
    assert branch_from_fix_versions([]) is None


def test_branch_from_fix_versions_unparseable():
    assert branch_from_fix_versions(["VHEL-9.6"]) is None


# ---------------------------------------------------------------------------
# _resolve_target_branch
# ---------------------------------------------------------------------------


def test_resolve_target_branch_prefers_summary(monkeypatch):
    from ymir.sweep.comment_parser import CommentData

    cd = CommentData(
        blocker_references=["RHEL-99"],
        pending_issues=["RHEL-99"],
        summary="Rebuild of pkg waiting for dep (nvr) to land in c9s buildroot",
        comment_id="1",
    )
    assert _resolve_target_branch(cd, ["rhel-10.0"]) == "c9s"


def test_resolve_target_branch_falls_back_to_fix_versions(monkeypatch):
    from ymir.sweep.comment_parser import CommentData

    cd = CommentData(
        blocker_references=["RHEL-99"],
        pending_issues=["RHEL-99"],
        summary="No branch indicator here",
        comment_id="1",
    )
    assert _resolve_target_branch(cd, ["rhel-10.2"]) == "c10s"


def test_resolve_target_branch_returns_none_when_undetermined(monkeypatch):
    from ymir.sweep.comment_parser import CommentData

    cd = CommentData(
        blocker_references=None,
        pending_issues=[],
        summary=None,
        comment_id="1",
    )
    assert _resolve_target_branch(cd, []) is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_blocker(fixed_in_build=None, components=None):
    """Build a minimal Issue representing the blocker."""
    from ymir.supervisor.supervisor_types import Issue

    return Issue(
        key="RHEL-67890",
        url="https://jira.example.com/browse/RHEL-67890",
        summary="Blocker issue",
        components=components if components is not None else ["golang"],
        status=IssueStatus.IN_PROGRESS,
        labels=[],
        fix_versions=["rhel-9.7.z"],
        errata_link=None,
        fixed_in_build=fixed_in_build,
    )


def _make_comment(blocker_ref="RHEL-67890", pending=None, summary=None):
    return make_ymir_comment(
        summary=summary or "Rebuild of pkg waiting for dep to land in c9s buildroot",
        pending_issues=pending or ["RHEL-67890"],
        blocker_reference=blocker_ref,
    )


def _make_issue_with_comment(**kw):
    return make_issue(comments=[_make_comment(**kw)])


# ---------------------------------------------------------------------------
# DependencySweep.is_unblocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unblocked_when_build_in_buildroot(monkeypatch):
    blocker = _make_blocker(fixed_in_build="golang-1.21.0-1.el9")
    monkeypatch.setattr("ymir.sweep.dependency.get_issue", lambda key, full=False: blocker)
    monkeypatch.setattr(
        "ymir.sweep.dependency.check_build_in_buildroot",
        lambda *a, **kw: _async_true(),
    )

    issue = _make_issue_with_comment()
    comment_data = _parse_comment(issue)
    result = await DependencySweep().is_unblocked(issue, comment_data)

    assert result.action == "unblocked"
    assert "RHEL-67890" in result.detail
    assert "golang-1.21.0-1.el9" in result.detail


@pytest.mark.asyncio
async def test_still_blocked_when_no_fixed_in_build(monkeypatch):
    blocker = _make_blocker(fixed_in_build=None)
    monkeypatch.setattr("ymir.sweep.dependency.get_issue", lambda key, full=False: blocker)

    issue = _make_issue_with_comment()
    result = await DependencySweep().is_unblocked(issue, _parse_comment(issue))

    assert result.action == "still_blocked"


@pytest.mark.asyncio
async def test_still_blocked_when_build_not_in_buildroot(monkeypatch):
    blocker = _make_blocker(fixed_in_build="golang-1.21.0-1.el9")
    monkeypatch.setattr("ymir.sweep.dependency.get_issue", lambda key, full=False: blocker)
    monkeypatch.setattr(
        "ymir.sweep.dependency.check_build_in_buildroot",
        lambda *a, **kw: _async_false(),
    )

    issue = _make_issue_with_comment()
    result = await DependencySweep().is_unblocked(issue, _parse_comment(issue))

    assert result.action == "still_blocked"


@pytest.mark.asyncio
async def test_error_when_blocker_key_missing(monkeypatch):
    comment = make_ymir_comment(
        summary="No blocker here",
        pending_issues=[],
        blocker_reference=None,
    )
    issue = make_issue(comments=[comment])
    from ymir.sweep.comment_parser import CommentData

    comment_data = CommentData(
        blocker_references=None,
        pending_issues=[],
        summary="No blocker here",
        comment_id="1",
    )

    result = await DependencySweep().is_unblocked(issue, comment_data)

    assert result.action == "error"


@pytest.mark.asyncio
async def test_error_when_blocker_not_jira_key(monkeypatch):
    from ymir.sweep.comment_parser import CommentData

    issue = make_issue()
    comment_data = CommentData(
        blocker_references=["not-a-jira-key"],
        pending_issues=[],
        summary="test",
        comment_id="1",
    )

    result = await DependencySweep().is_unblocked(issue, comment_data)

    assert result.action == "error"


@pytest.mark.asyncio
async def test_error_when_get_issue_raises_http_error(monkeypatch):
    def raise_http(*a, **kw):
        resp = flexmock(status_code=404)
        raise requests.HTTPError("Not found", response=resp)

    monkeypatch.setattr("ymir.sweep.dependency.get_issue", raise_http)

    issue = _make_issue_with_comment()
    result = await DependencySweep().is_unblocked(issue, _parse_comment(issue))

    assert result.action == "error"


@pytest.mark.asyncio
async def test_error_when_blocker_has_no_components(monkeypatch):
    blocker = _make_blocker(fixed_in_build="golang-1.21.0-1.el9", components=[])
    monkeypatch.setattr("ymir.sweep.dependency.get_issue", lambda key, full=False: blocker)

    issue = _make_issue_with_comment()
    result = await DependencySweep().is_unblocked(issue, _parse_comment(issue))

    assert result.action == "error"


@pytest.mark.asyncio
async def test_error_when_check_build_raises(monkeypatch):
    blocker = _make_blocker(fixed_in_build="golang-1.21.0-1.el9")
    monkeypatch.setattr("ymir.sweep.dependency.get_issue", lambda key, full=False: blocker)
    monkeypatch.setattr(
        "ymir.sweep.dependency.check_build_in_buildroot",
        lambda *a, **kw: _async_raise(RuntimeError("Koji unreachable")),
    )

    issue = _make_issue_with_comment()
    result = await DependencySweep().is_unblocked(issue, _parse_comment(issue))

    assert result.action == "error"
    assert "Koji unreachable" in result.detail


@pytest.mark.asyncio
async def test_falls_back_to_pending_issues_when_no_blocker_reference(monkeypatch):
    """Blocker key resolved from pending_issues when blocker_reference is None."""
    blocker = _make_blocker(fixed_in_build="golang-1.21.0-1.el9")
    monkeypatch.setattr("ymir.sweep.dependency.get_issue", lambda key, full=False: blocker)
    monkeypatch.setattr(
        "ymir.sweep.dependency.check_build_in_buildroot",
        lambda *a, **kw: _async_true(),
    )

    from ymir.sweep.comment_parser import CommentData

    issue = make_issue()
    comment_data = CommentData(
        blocker_references=None,
        pending_issues=["RHEL-67890"],
        summary="Rebuild of pkg waiting for dep to land in c9s buildroot",
        comment_id="1",
    )

    result = await DependencySweep().is_unblocked(issue, comment_data)

    assert result.action == "unblocked"


# ---------------------------------------------------------------------------
# fix_version normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_fix_version_normalised_before_buildroot_check(monkeypatch):
    """A stale Y-stream fixVersion (rhel-9.8) must be normalised to rhel-9.8.z.

    Without normalisation ``_resolve_buildroot_checks`` treats it as a Y-stream
    version and only checks the CS Koji buildroot, missing the Brew Z-stream
    check that triage performs after normalising.
    """
    rhel_config = {"current_y_streams": {"9": "rhel-9.9"}, "current_z_streams": {"9": "rhel-9.8.z"}}

    async def fake_load_rhel_config():
        return rhel_config

    captured: dict = {}

    async def capture_buildroot_check(*a, fix_version="", **kw):
        captured["fix_version"] = fix_version
        return True

    monkeypatch.setattr("ymir.sweep.dependency.load_rhel_config", fake_load_rhel_config)
    monkeypatch.setattr(
        "ymir.sweep.dependency.get_issue",
        lambda key, full=False: _make_blocker(fixed_in_build="golang-1.21.0-1.el9"),
    )
    monkeypatch.setattr("ymir.sweep.dependency.check_build_in_buildroot", capture_buildroot_check)

    issue = _make_issue_with_comment()
    issue = make_issue(fix_versions=["rhel-9.8"], comments=[_make_comment()])
    result = await DependencySweep().is_unblocked(issue, _parse_comment(issue))

    assert result.action == "unblocked"
    assert captured.get("fix_version") == "rhel-9.8.z", (
        f"Expected normalised fix_version 'rhel-9.8.z', got {captured.get('fix_version')!r}"
    )


@pytest.mark.asyncio
async def test_current_y_stream_fix_version_not_normalised(monkeypatch):
    """A current Y-stream fixVersion must be passed through unchanged."""
    rhel_config = {"current_y_streams": {"9": "rhel-9.9"}, "current_z_streams": {"9": "rhel-9.8.z"}}

    async def fake_load_rhel_config():
        return rhel_config

    captured: dict = {}

    async def capture_buildroot_check(*a, fix_version="", **kw):
        captured["fix_version"] = fix_version
        return True

    monkeypatch.setattr("ymir.sweep.dependency.load_rhel_config", fake_load_rhel_config)
    monkeypatch.setattr(
        "ymir.sweep.dependency.get_issue",
        lambda key, full=False: _make_blocker(fixed_in_build="golang-1.21.0-1.el9"),
    )
    monkeypatch.setattr("ymir.sweep.dependency.check_build_in_buildroot", capture_buildroot_check)

    issue = make_issue(fix_versions=["rhel-9.9"], comments=[_make_comment()])
    result = await DependencySweep().is_unblocked(issue, _parse_comment(issue))

    assert result.action == "unblocked"
    assert captured.get("fix_version") == "rhel-9.9", (
        f"Expected unchanged fix_version 'rhel-9.9', got {captured.get('fix_version')!r}"
    )


@pytest.mark.asyncio
async def test_error_when_fix_version_missing_but_branch_from_summary(monkeypatch):
    """Branch resolvable from the comment summary while fix_versions is empty.

    This can only happen if the Fix Version was cleared after triage. Without
    it the Z-stream signal is lost and only one buildroot would be checked, so
    the sweep fails loud with an error instead of silently degrading.
    """
    monkeypatch.setattr(
        "ymir.sweep.dependency.get_issue",
        lambda key, full=False: _make_blocker(fixed_in_build="golang-1.21.0-1.el9"),
    )
    # Summary supplies the branch (c9s); fix_versions is empty.
    issue = make_issue(fix_versions=[], comments=[_make_comment()])

    result = await DependencySweep().is_unblocked(issue, _parse_comment(issue))

    assert result.action == "error"
    assert "Fix Version" in result.detail
    assert _resolve_target_branch(_parse_comment(issue), issue.fix_versions) == "c9s"


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def _async_true(*a, **kw):
    return True


async def _async_false(*a, **kw):
    return False


async def _async_raise(exc):
    raise exc


def _parse_comment(issue):
    from ymir.sweep.comment_parser import parse_ymir_comment

    return parse_ymir_comment(issue)
