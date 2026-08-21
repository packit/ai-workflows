"""Unit tests for ymir.sweep.pr_pending.PRPendingSweep."""

import pytest

from ymir.supervisor.supervisor_types import MergeRequestState
from ymir.sweep.comment_parser import CommentData
from ymir.sweep.pr_pending import _GITHUB_PR_RE, _GITLAB_MR_RE, PRPendingSweep
from ymir.sweep.tests.unit.conftest import make_issue

# ---------------------------------------------------------------------------
# URL regexes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected_host,expected_path,expected_iid",
    [
        (
            "https://gitlab.com/redhat/centos-stream/rpms/pkg/-/merge_requests/42",
            "gitlab.com",
            "redhat/centos-stream/rpms/pkg",
            "42",
        ),
        (
            "https://gitlab.cee.redhat.com/foo/bar/-/merge_requests/7",
            "gitlab.cee.redhat.com",
            "foo/bar",
            "7",
        ),
        (
            "https://gitlab.com/group/sub/repo/-/merge_requests/100",
            "gitlab.com",
            "group/sub/repo",
            "100",
        ),
    ],
)
def test_gitlab_mr_re_matches_valid_urls(url, expected_host, expected_path, expected_iid):
    m = _GITLAB_MR_RE.match(url)
    assert m is not None
    assert m.group(1) == expected_host
    assert m.group(2) == expected_path
    assert m.group(3) == expected_iid


def test_gitlab_mr_re_rejects_non_mr_urls():
    assert _GITLAB_MR_RE.match("https://gitlab.com/group/repo") is None
    assert _GITLAB_MR_RE.match("https://github.com/foo/bar/pull/1") is None


@pytest.mark.parametrize(
    "url",
    [
        # Attacker-controlled host that merely starts with "gitlab." must not match,
        # otherwise the GITLAB_TOKEN would be sent there via gitlab_api_get.
        "https://gitlab.attacker.com/foo/bar/-/merge_requests/1",
        "https://gitlab.com.attacker.com/foo/bar/-/merge_requests/1",
        "https://gitlab.evil.example/foo/bar/-/merge_requests/1",
    ],
)
def test_gitlab_mr_re_rejects_untrusted_hosts(url):
    assert _GITLAB_MR_RE.match(url) is None


@pytest.mark.asyncio
async def test_error_when_blocker_reference_points_at_untrusted_gitlab_host(monkeypatch):
    """A hostile gitlab.* host is reported as unrecognisable, never reaching the API."""

    def _fail(*_args, **_kwargs):
        raise AssertionError("gitlab_api_get must not be called for an untrusted host")

    monkeypatch.setattr("ymir.sweep.pr_pending.gitlab_api_get", _fail)

    issue = make_issue()
    cd = _comment_data(blocker_reference="https://gitlab.attacker.com/foo/bar/-/merge_requests/1")
    result = await PRPendingSweep().is_unblocked(issue, cd)

    assert result.action == "error"
    assert "not a recognisable" in result.detail


@pytest.mark.parametrize(
    "url,expected_owner_repo,expected_pr",
    [
        ("https://github.com/foo/bar/pull/1", "foo/bar", "1"),
        ("https://github.com/org/repo/pull/999", "org/repo", "999"),
    ],
)
def test_github_pr_re_matches_valid_urls(url, expected_owner_repo, expected_pr):
    m = _GITHUB_PR_RE.match(url)
    assert m is not None
    assert m.group(1) == expected_owner_repo
    assert m.group(2) == expected_pr


def test_github_pr_re_rejects_non_pr_urls():
    assert _GITHUB_PR_RE.match("https://github.com/foo/bar/issues/1") is None
    assert _GITHUB_PR_RE.match("https://gitlab.com/foo/bar/-/merge_requests/1") is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GITLAB_MR_URL = "https://gitlab.com/redhat/centos-stream/rpms/pkg/-/merge_requests/42"
_GITHUB_PR_URL = "https://github.com/upstream-org/upstream-pkg/pull/123"


def _comment_data(blocker_reference=_GITLAB_MR_URL):
    return CommentData(
        blocker_references=[blocker_reference] if blocker_reference else None,
        pending_issues=["RHEL-12345"],
        summary="Upstream patch not yet merged",
        comment_id="1",
    )


def _mock_gitlab_api_get(state: str, monkeypatch):
    monkeypatch.setattr(
        "ymir.sweep.pr_pending.gitlab_api_get",
        lambda path, *, gitlab_url=None, params=None: {"state": state},
    )


def _mock_github_api_get(state: str, merged_at: str | None, monkeypatch):
    monkeypatch.setattr(
        "ymir.sweep.pr_pending.github_api_get",
        lambda path, **_kw: {"state": state, "merged_at": merged_at},
    )


# ---------------------------------------------------------------------------
# PRPendingSweep.is_unblocked — GitLab path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unblocked_when_gitlab_mr_merged(monkeypatch):
    _mock_gitlab_api_get(MergeRequestState.MERGED, monkeypatch)

    issue = make_issue()
    result = await PRPendingSweep().is_unblocked(issue, _comment_data())

    assert result.action == "unblocked"
    assert _GITLAB_MR_URL in result.detail


@pytest.mark.asyncio
async def test_still_blocked_when_gitlab_mr_open(monkeypatch):
    _mock_gitlab_api_get(MergeRequestState.OPEN, monkeypatch)

    issue = make_issue()
    result = await PRPendingSweep().is_unblocked(issue, _comment_data())

    assert result.action == "still_blocked"
    assert "opened" in result.detail


@pytest.mark.asyncio
async def test_transition_to_no_patch_when_gitlab_mr_closed(monkeypatch):
    _mock_gitlab_api_get(MergeRequestState.CLOSED, monkeypatch)

    transitions = []

    def capture_transition(issue_key, new_label, comment=None):
        transitions.append((issue_key, new_label))

    strategy = PRPendingSweep()
    strategy.on_transition = capture_transition

    issue = make_issue(key="RHEL-55555")
    result = await strategy.is_unblocked(issue, _comment_data())

    assert result.action == "transitioned"
    assert len(transitions) == 1
    from ymir.common.constants import JiraLabels

    assert transitions[0] == ("RHEL-55555", JiraLabels.YMIR_POSTPONED_NO_PATCH)


@pytest.mark.asyncio
async def test_error_when_blocker_reference_absent(monkeypatch):
    issue = make_issue()
    cd = CommentData(
        blocker_references=None,
        pending_issues=["RHEL-12345"],
        summary="No URL",
        comment_id="1",
    )

    result = await PRPendingSweep().is_unblocked(issue, cd)

    assert result.action == "error"


@pytest.mark.asyncio
async def test_error_when_url_matches_no_known_platform(monkeypatch):
    issue = make_issue()
    cd = _comment_data(blocker_reference="https://bitbucket.org/foo/bar/pull-requests/1")

    result = await PRPendingSweep().is_unblocked(issue, cd)

    assert result.action == "error"
    assert "not a recognisable" in result.detail


@pytest.mark.asyncio
async def test_still_blocked_for_unknown_gitlab_mr_state(monkeypatch):
    """An unrecognised MR state falls through to still_blocked."""
    _mock_gitlab_api_get("locked", monkeypatch)

    issue = make_issue()
    result = await PRPendingSweep().is_unblocked(issue, _comment_data())

    assert result.action == "still_blocked"
    assert "locked" in result.detail


@pytest.mark.asyncio
async def test_error_when_gitlab_api_call_fails(monkeypatch):
    def raise_error(path, *, gitlab_url=None, params=None):
        raise Exception("Network timeout")

    monkeypatch.setattr("ymir.sweep.pr_pending.gitlab_api_get", raise_error)

    issue = make_issue()
    result = await PRPendingSweep().is_unblocked(issue, _comment_data())

    assert result.action == "error"
    assert "Network timeout" in result.detail


# ---------------------------------------------------------------------------
# PRPendingSweep.is_unblocked — GitHub path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unblocked_when_github_pr_merged(monkeypatch):
    _mock_github_api_get("closed", "2025-01-15T12:00:00Z", monkeypatch)

    issue = make_issue()
    result = await PRPendingSweep().is_unblocked(issue, _comment_data(_GITHUB_PR_URL))

    assert result.action == "unblocked"
    assert _GITHUB_PR_URL in result.detail


@pytest.mark.asyncio
async def test_still_blocked_when_github_pr_open(monkeypatch):
    _mock_github_api_get("open", None, monkeypatch)

    issue = make_issue()
    result = await PRPendingSweep().is_unblocked(issue, _comment_data(_GITHUB_PR_URL))

    assert result.action == "still_blocked"
    assert "opened" in result.detail


@pytest.mark.asyncio
async def test_transition_to_no_patch_when_github_pr_closed_without_merge(monkeypatch):
    _mock_github_api_get("closed", None, monkeypatch)

    transitions = []

    def capture_transition(issue_key, new_label, comment=None):
        transitions.append((issue_key, new_label))

    strategy = PRPendingSweep()
    strategy.on_transition = capture_transition

    issue = make_issue(key="RHEL-55555")
    result = await strategy.is_unblocked(issue, _comment_data(_GITHUB_PR_URL))

    assert result.action == "transitioned"
    assert len(transitions) == 1
    from ymir.common.constants import JiraLabels

    assert transitions[0] == ("RHEL-55555", JiraLabels.YMIR_POSTPONED_NO_PATCH)


@pytest.mark.asyncio
async def test_error_when_github_api_call_fails(monkeypatch):
    monkeypatch.setattr(
        "ymir.sweep.pr_pending.github_api_get",
        lambda path, **_kw: (_ for _ in ()).throw(Exception("Connection refused")),
    )

    issue = make_issue()
    result = await PRPendingSweep().is_unblocked(issue, _comment_data(_GITHUB_PR_URL))

    assert result.action == "error"
    assert "Connection refused" in result.detail


# ---------------------------------------------------------------------------
# API call construction — GitLab
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_api_get_called_with_correct_path_and_host(monkeypatch):
    captured = {}

    def capture(path, *, gitlab_url=None, params=None):
        captured["path"] = path
        captured["gitlab_url"] = gitlab_url
        return {"state": "opened"}

    monkeypatch.setattr("ymir.sweep.pr_pending.gitlab_api_get", capture)

    issue = make_issue()
    await PRPendingSweep().is_unblocked(
        issue,
        _comment_data("https://gitlab.com/redhat/centos-stream/rpms/pkg/-/merge_requests/42"),
    )

    assert captured["gitlab_url"] == "https://gitlab.com"
    assert "merge_requests/42" in captured["path"]
    assert "redhat" in captured["path"]


@pytest.mark.asyncio
async def test_gitlab_api_get_called_with_cee_host(monkeypatch):
    captured = {}

    def capture(path, *, gitlab_url=None, params=None):
        captured["path"] = path
        captured["gitlab_url"] = gitlab_url
        return {"state": "merged"}

    monkeypatch.setattr("ymir.sweep.pr_pending.gitlab_api_get", capture)

    issue = make_issue()
    await PRPendingSweep().is_unblocked(
        issue,
        _comment_data("https://gitlab.cee.redhat.com/foo/bar/-/merge_requests/7"),
    )

    assert captured["gitlab_url"] == "https://gitlab.cee.redhat.com"
    assert "merge_requests/7" in captured["path"]


# ---------------------------------------------------------------------------
# API call construction — GitHub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_api_get_called_with_correct_path(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "ymir.sweep.pr_pending.github_api_get",
        lambda path, **_kw: captured.update({"path": path}) or {"state": "open", "merged_at": None},
    )

    issue = make_issue()
    await PRPendingSweep().is_unblocked(
        issue,
        _comment_data("https://github.com/upstream-org/upstream-pkg/pull/123"),
    )

    assert captured["path"] == "repos/upstream-org/upstream-pkg/pulls/123"
