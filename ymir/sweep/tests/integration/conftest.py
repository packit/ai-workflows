"""Shared fixtures and helpers for sweep integration tests."""

import pytest
from flexmock import flexmock

from ymir.sweep.tests.unit.conftest import (  # noqa: F401
    _DEFAULT_RHEL_CONFIG,
    make_issue,
    make_ymir_comment,
)


@pytest.fixture(autouse=True)
def _inject_jira_email(monkeypatch):
    """Inject JIRA_EMAIL for all sweep integration tests.

    parse_ymir_comment() reads JIRA_EMAIL at call time and raises OSError
    when it is absent.  The value must match the authorEmail set by
    make_ymir_comment() so that comment look-ups succeed.
    """
    monkeypatch.setenv("JIRA_EMAIL", "ymir@redhat.com")


@pytest.fixture(autouse=True)
def _stub_load_rhel_config(monkeypatch):
    """Stub load_rhel_config for all sweep integration tests.

    DependencySweep calls load_rhel_config() to normalise fix_version before
    the buildroot check.  Integration tests don't have a real
    rhel-config.json on disk, so we return the same default config used by
    the unit tests.

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


@pytest.fixture
def captured_label_ops(monkeypatch):
    """Patch label operations in base and no_patch modules; return captured calls.

    Returns a dict with keys ``"remove"`` and ``"add"``, each a list of
    ``(issue_key, label)`` tuples recorded in call order.  Patches both
    ``ymir.sweep.base`` (used by ``on_unblock``/``on_transition``) and
    ``ymir.sweep.no_patch`` (which calls ``remove_issue_label`` directly).
    """
    ops: dict[str, list[tuple[str, str]]] = {"remove": [], "add": []}

    def capture_remove(key: str, label: str, comment: str | None = None) -> None:
        ops["remove"].append((key, label))

    def capture_add(key: str, label: str, comment: str | None = None) -> None:
        ops["add"].append((key, label))

    monkeypatch.setattr("ymir.sweep.base.remove_issue_label", capture_remove)
    monkeypatch.setattr("ymir.sweep.base.add_issue_label", capture_add)
    monkeypatch.setattr("ymir.sweep.no_patch.remove_issue_label", capture_remove)
    monkeypatch.setattr("ymir.sweep.no_patch.add_issue_label", capture_add)
    return ops


def make_redis() -> tuple:
    """Return a (redis_mock, pushed_list) pair for asserting Redis side effects.

    ``pushed_list`` accumulates the queue name on each ``lpush`` call so tests
    can assert both call count and the target queue without flexmock expectation
    matching.
    """
    pushed: list[str] = []
    r = flexmock()
    r.should_receive("lpush").replace_with(lambda queue, payload: pushed.append(queue))
    return r, pushed
