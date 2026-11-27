import pytest
from flexmock import flexmock

from ymir.agents.rebuild_agent import _clear_rebuild_resolved_errors
from ymir.common.models import ConsolidatedIssue, RebuildData


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_successful_consolidated_rebuild_clears_primary_and_distinct_siblings(dry_run):
    calls = []

    async def mock_clear(redis_conn, issue, queue, *, target_branch, dry_run):
        calls.append((redis_conn, issue, queue, target_branch, dry_run))

    from ymir.agents import rebuild_agent

    flexmock(rebuild_agent).should_receive("clear_resolved_errors").replace_with(mock_clear).once()
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

    assert calls == [(redis, data.all_jira_issues, "rebuild_queue_c10s", "rhel-10.3", dry_run)]
