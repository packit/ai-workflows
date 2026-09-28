import pytest

from ymir.agents.rebuild_agent import _clear_rebuild_resolved_errors
from ymir.agents.rebuild_consolidation import build_rebuild_siblings_jql
from ymir.common.models import ConsolidatedIssue, RebuildData


def test_build_rebuild_siblings_jql():
    jql = build_rebuild_siblings_jql("RHEL-100", "git-lfs", "rhel-9.8")
    assert 'component = "git-lfs"' in jql
    assert 'fixVersion in ("rhel-9.8", "rhel-9.8.z")' in jql
    assert 'key != "RHEL-100"' in jql
    assert 'labels = "SecurityTracking"' in jql
    assert "labels not in" in jql
    assert '"ymir_triaged_rebuild"' not in jql
    assert '"ymir_rebuilt"' not in jql
    assert '"ymir_triaged_not_affected"' in jql
    assert '"ymir_triaged_backport"' in jql
    assert '"ymir_triaged_rebase"' in jql
    assert 'status in ("New", "Planning")' in jql


def test_build_rebuild_siblings_jql_filters_modular_stream():
    jql = build_rebuild_siblings_jql(
        "RHEL-100", "postgis", "rhel-9.8", downstream_component="postgresql:16/postgis"
    )
    assert 'cf[10669] = "postgresql:16/postgis"' in jql
    assert 'component = "postgis"' in jql


def test_build_rebuild_siblings_jql_no_filter_for_nonmodular():
    jql = build_rebuild_siblings_jql("RHEL-100", "curl", "rhel-9.8", downstream_component="curl")
    assert "cf[10669]" not in jql


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_successful_consolidated_rebuild_clears_primary_and_distinct_siblings(monkeypatch, dry_run):
    calls = []

    async def mock_clear(redis_conn, issue, queue, *, target_branch, dry_run):
        calls.append((redis_conn, issue, queue, target_branch, dry_run))

    monkeypatch.setattr("ymir.agents.rebuild_agent.clear_resolved_errors", mock_clear)
    redis = object()
    data = RebuildData(
        package="git-lfs",
        jira_issue="RHEL-100",
        consolidated_issues=[
            ConsolidatedIssue(issue_key="RHEL-200"),
            ConsolidatedIssue(issue_key="RHEL-100"),
            ConsolidatedIssue(issue_key="RHEL-200"),
            ConsolidatedIssue(issue_key="RHEL-300"),
        ],
    )

    await _clear_rebuild_resolved_errors(
        redis, data, "rebuild_queue_c10s", target_branch="rhel-10.3", dry_run=dry_run
    )

    assert calls == [
        (redis, issue, "rebuild_queue_c10s", "rhel-10.3", dry_run)
        for issue in ("RHEL-100", "RHEL-200", "RHEL-300")
    ]


def test_build_rebuild_siblings_jql_escapes_component_quotes():
    jql = build_rebuild_siblings_jql("RHEL-100", 'comp"name', "rhel-9.8.z")
    assert r'component = "comp\"name"' in jql
    assert 'fixVersion in ("rhel-9.8", "rhel-9.8.z")' in jql
