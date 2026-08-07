"""Unit tests for ymir.sweep.comment_parser."""

from datetime import datetime

from ymir.supervisor.supervisor_types import JiraComment
from ymir.sweep.comment_parser import parse_ymir_comment
from ymir.sweep.tests.unit.conftest import make_issue, make_ymir_comment


def test_parse_dependency_comment_with_blocker():
    comment = make_ymir_comment(
        resolution="postponed_dependency",
        summary="Rebuild of pkg waiting for dep (nvr) to land in c9s buildroot",
        pending_issues=["RHEL-67890"],
        blocker_reference="RHEL-67890",
    )
    issue = make_issue(comments=[comment])

    result = parse_ymir_comment(issue)

    assert result is not None
    assert result.blocker_references == ["RHEL-67890"]
    assert result.pending_issues == ["RHEL-67890"]
    assert "c9s buildroot" in result.summary
    assert result.comment_id == "100001"


def test_parse_y_stream_comment_multiple_pending():
    comment = make_ymir_comment(
        resolution="postponed_y_stream",
        summary="Y-stream CVE waiting for Z-stream clones to ship",
        pending_issues=["RHEL-11111", "RHEL-22222"],
    )
    issue = make_issue(comments=[comment])

    result = parse_ymir_comment(issue)

    assert result is not None
    assert result.blocker_references is None
    assert result.pending_issues == ["RHEL-11111", "RHEL-22222"]


def test_parse_pr_pending_comment_with_mr_url():
    mr_url = "https://gitlab.com/redhat/centos-stream/rpms/pkg/-/merge_requests/42"
    comment = make_ymir_comment(
        resolution="postponed_pr_pending",
        summary="Upstream patch not yet merged",
        pending_issues=["RHEL-55555"],
        blocker_reference=mr_url,
    )
    issue = make_issue(comments=[comment])

    result = parse_ymir_comment(issue)

    assert result is not None
    assert result.blocker_references == [mr_url]


def test_parse_no_patch_comment():
    comment = make_ymir_comment(
        resolution="postponed_no_patch",
        summary="No upstream patch is available for this CVE",
    )
    issue = make_issue(comments=[comment])

    result = parse_ymir_comment(issue)

    assert result is not None
    assert result.pending_issues == []
    assert result.blocker_references is None


def test_returns_none_for_issue_with_no_ymir_comment():
    human_comment = JiraComment(
        id="1",
        authorName="human",
        authorEmail="human@example.com",
        created=datetime(2025, 1, 1),
        body="This is just a human comment, no Ymir marker.",
    )
    issue = make_issue(comments=[human_comment])

    assert parse_ymir_comment(issue) is None


def test_returns_none_for_issue_with_no_comments():
    issue = make_issue(comments=[])

    assert parse_ymir_comment(issue) is None


def test_parses_ymir_comment_without_optional_fields():
    """A Ymir comment is parseable even when blocker/pending fields are absent.

    The postponement reason lives in the issue's label, not the comment, so
    the parser no longer requires any reason field to be present.
    """
    comment = JiraComment(
        id="1",
        authorName="ymir-bot",
        authorEmail="ymir@redhat.com",
        created=datetime(2025, 6, 15),
        body=(
            "Output from Ymir Triage Agent: \n\n"
            "*Resolution*: postponed_no_patch\n"
            "*Summary*: Something\n"
            "Warning: AI-generated content."
        ),
    )
    issue = make_issue(comments=[comment])

    result = parse_ymir_comment(issue)

    assert result is not None
    assert result.summary == "Something"
    assert result.blocker_references is None
    assert result.pending_issues == []


def test_returns_latest_ymir_comment_when_multiple_exist():
    older = make_ymir_comment(
        resolution="postponed_dependency",
        summary="Old reason",
        comment_id="10001",
        pending_issues=["RHEL-1"],
    )
    newer = make_ymir_comment(
        resolution="postponed_y_stream",
        summary="Newer reason",
        comment_id="10002",
        pending_issues=["RHEL-2"],
    )
    # Give newer a later created timestamp
    newer_comment = JiraComment(
        id="10002",
        authorName="ymir-bot",
        authorEmail="ymir@redhat.com",
        created=datetime(2025, 12, 1),
        body=newer.body,
    )
    older_comment = JiraComment(
        id="10001",
        authorName="ymir-bot",
        authorEmail="ymir@redhat.com",
        created=datetime(2025, 6, 1),
        body=older.body,
    )
    issue = make_issue(comments=[older_comment, newer_comment])

    result = parse_ymir_comment(issue)

    assert result is not None
    assert result.comment_id == "10002"
    assert result.summary == "Newer reason"


def test_ignores_non_ymir_comments_between_ymir_comments():
    ymir_comment = make_ymir_comment(
        resolution="postponed_dependency",
        summary="Rebuild waiting",
        comment_id="1",
        pending_issues=["RHEL-99"],
        blocker_reference="RHEL-99",
    )
    human_comment = JiraComment(
        id="2",
        authorName="maintainer",
        authorEmail=None,
        created=datetime(2025, 8, 1),
        body="I've looked at this, still waiting.",
    )
    issue = make_issue(comments=[ymir_comment, human_comment])

    result = parse_ymir_comment(issue)

    assert result is not None
    assert result.comment_id == "1"
    assert result.summary == "Rebuild waiting"
