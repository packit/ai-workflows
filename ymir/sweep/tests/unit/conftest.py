"""Shared fixtures for sweep unit tests."""

from datetime import datetime

import pytest

from ymir.agents.constants import JIRA_COMMENT_TEMPLATE
from ymir.common.models import PostponedData, Resolution, TriageOutputSchema
from ymir.supervisor.supervisor_types import FullIssue, IssueStatus, JiraComment


def make_issue(
    key: str = "RHEL-12345",
    labels: list[str] | None = None,
    fix_versions: list[str] | None = None,
    components: list[str] | None = None,
    fixed_in_build: str | None = None,
    comments: list[JiraComment] | None = None,
) -> FullIssue:
    """Build a minimal FullIssue for testing."""
    return FullIssue(
        key=key,
        url=f"https://jira.example.com/browse/{key}",
        summary="Test CVE issue",
        components=components if components is not None else ["test-component"],
        status=IssueStatus.NEW,
        labels=labels if labels is not None else [],
        fix_versions=fix_versions if fix_versions is not None else ["rhel-9.6.0"],
        errata_link=None,
        fixed_in_build=fixed_in_build,
        description="Test description",
        comments=comments if comments is not None else [],
    )


def make_ymir_comment(
    resolution: str = "postponed_dependency",
    summary: str = "Test postponement summary",
    pending_issues: list[str] | None = None,
    blocker_reference: str | None = None,
    comment_id: str = "100001",
) -> JiraComment:
    """Build a Ymir triage comment using the real production formatting path.

    Delegates to ``TriageOutputSchema.format_for_comment()`` and wraps the
    result in ``JIRA_COMMENT_TEMPLATE``, exactly as ``tasks.comment_in_jira``
    does at runtime. Any change to the comment format automatically propagates
    to all tests that use this fixture.
    """
    data = PostponedData(
        summary=summary,
        pending_issues=pending_issues or [],
        jira_issue="RHEL-12345",
        blocker_references=[blocker_reference] if blocker_reference else None,
    )
    body = JIRA_COMMENT_TEMPLATE.substitute(
        AGENT_TYPE="Triage",
        JIRA_COMMENT=TriageOutputSchema(resolution=Resolution(resolution), data=data).format_for_comment(),
    )
    return JiraComment(
        id=comment_id,
        authorName="ymir-bot",
        authorEmail="ymir@redhat.com",
        created=datetime(2025, 6, 15),
        body=body,
    )


_DEFAULT_RHEL_CONFIG = {
    "current_y_streams": {
        "8": "rhel-8.10",
        "9": "rhel-9.6.0",
        "10": "rhel-10.0",
    },
    "current_z_streams": {
        "8": "rhel-8.10.z",
        "9": "rhel-9.6.z",
        "10": "rhel-10.0.z",
    },
}


@pytest.fixture(autouse=True)
def _inject_jira_email(monkeypatch):
    """Inject JIRA_EMAIL for all sweep unit tests.

    parse_ymir_comment() reads JIRA_EMAIL at call time and raises OSError
    when it is absent.  The value must match the authorEmail set by
    make_ymir_comment() so that comment look-ups succeed.
    """
    monkeypatch.setenv("JIRA_EMAIL", "ymir@redhat.com")


@pytest.fixture(autouse=True)
def _stub_load_rhel_config(monkeypatch):
    """Stub load_rhel_config for all sweep unit tests.

    DependencySweep calls load_rhel_config() to normalise fix_version before
    the buildroot check.  Tests that don't exercise normalisation directly
    shouldn't need a real rhel-config.json on disk.

    Tests that do exercise normalisation override this stub via their own
    monkeypatch call, which takes precedence for the duration of that test.

    (YStreamSweep no longer calls load_rhel_config — it delegates to the
    eligibility tool — so only the dependency module is patched here.)
    """

    async def _default_config():
        return _DEFAULT_RHEL_CONFIG

    monkeypatch.setattr("ymir.sweep.dependency.load_rhel_config", _default_config)


@pytest.fixture
def mock_env(monkeypatch):
    """Set minimal required environment variables."""
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "ymir@redhat.com")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("GITLAB_TOKEN", "test-gitlab-token")
