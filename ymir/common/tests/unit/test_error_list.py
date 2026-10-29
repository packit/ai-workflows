import pytest

from ymir.common.constants import RedisQueues
from ymir.common.error_list import ERROR_CLEANUP_SCAN_LIMIT, clear_resolved_errors
from ymir.common.models import ErrorData, ErrorListEntry, Task


class ListRedis:
    def __init__(self, entries):
        self.entries = entries
        self.lrange_calls = 0
        self.lrem_calls = 0

    async def lrange(self, key, start, stop):
        self.lrange_calls += 1
        assert (key, start, stop) == (RedisQueues.ERROR_LIST.value, 0, ERROR_CLEANUP_SCAN_LIMIT - 1)
        return self.entries[start : stop + 1]

    async def lrem(self, key, count, value):
        self.lrem_calls += 1
        assert (key, count) == (RedisQueues.ERROR_LIST.value, 1)
        self.entries.remove(value)
        return 1


def entry(issue, queue, branch=None):
    task = Task(metadata={"target_branch": branch})
    return (
        ErrorListEntry(
            error_id=1,
            queue=queue,
            task=task,
            error=ErrorData(jira_issue=issue, details="failed"),
        )
        .model_dump_json()
        .encode()
    )


@pytest.mark.asyncio
async def test_success_clears_prior_errors_for_same_issue_workflow_and_branch():
    matching = entry("RHEL-1", "backport_queue_c9s_todo", "rhel-9.9.0")
    another_matching = entry("RHEL-1", "backport_queue_c9s", "rhel-9.9.0")
    other_branch = entry("RHEL-1", "backport_queue_c9s", "rhel-9.8.0")
    other_workflow = entry("RHEL-1", "rebase_queue_c9s", "rhel-9.9.0")
    other_issue = entry("RHEL-2", "backport_queue_c9s", "rhel-9.9.0")
    legacy = b'{"jira_issue":"RHEL-1","details":"old failure"}'
    redis = ListRedis([matching, another_matching, other_branch, other_workflow, other_issue, legacy])

    removed = await clear_resolved_errors(redis, "RHEL-1", "backport_queue_c9s", target_branch="rhel-9.9.0")

    assert removed == 2
    assert redis.entries == [other_branch, other_workflow, other_issue, legacy]


@pytest.mark.asyncio
@pytest.mark.parametrize("issues", ["RHEL-1", ["RHEL-1", "RHEL-2"]])
async def test_successful_dry_run_keeps_matching_production_error_entry(issues):
    matching = entry("RHEL-1", "backport_queue_c9s", "rhel-9.9.0")
    redis = ListRedis([matching])

    removed = await clear_resolved_errors(
        redis, issues, "backport_queue_c9s", target_branch="rhel-9.9.0", dry_run=True
    )

    assert removed == 0
    assert redis.entries == [matching]
    assert redis.lrange_calls == 0
    assert redis.lrem_calls == 0


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_fail_completed_workflow():
    class BrokenRedis:
        async def lrange(self, *_args):
            raise ConnectionError("Redis unavailable")

    assert await clear_resolved_errors(BrokenRedis(), "RHEL-1", "triage_queue") == 0


@pytest.mark.asyncio
async def test_batch_clears_all_resolved_issues_in_one_scan():
    primary = entry("RHEL-1", "rebase_queue_c9s", "rhel-9.9.0")
    sibling = entry("RHEL-2", "rebase_queue_c9s_todo", "rhel-9.9.0")
    unresolved = entry("RHEL-3", "rebase_queue_c9s", "rhel-9.9.0")
    redis = ListRedis([primary, sibling, unresolved])

    removed = await clear_resolved_errors(
        redis, ["RHEL-1", "RHEL-2", "RHEL-1"], "rebase_queue_c9s", target_branch="rhel-9.9.0"
    )

    assert removed == 2
    assert redis.entries == [unresolved]
    assert redis.lrange_calls == 1


@pytest.mark.asyncio
async def test_cleanup_only_scans_recent_entries_without_discarding_history():
    recent = entry("RHEL-1", "triage_queue")
    unrelated = entry("RHEL-2", "triage_queue")
    old = entry("RHEL-1", "triage_queue_todo")
    redis = ListRedis([recent] + [unrelated] * (ERROR_CLEANUP_SCAN_LIMIT - 1) + [old])

    assert await clear_resolved_errors(redis, "RHEL-1", "triage_queue") == 1
    assert redis.entries == [unrelated] * (ERROR_CLEANUP_SCAN_LIMIT - 1) + [old]
    assert redis.lrange_calls == 1


@pytest.mark.asyncio
async def test_empty_batch_does_not_access_redis():
    redis = ListRedis([])

    assert await clear_resolved_errors(redis, [], "triage_queue") == 0
    assert redis.lrange_calls == 0


@pytest.mark.asyncio
async def test_malformed_entries_do_not_prevent_cleanup():
    malformed = [b"invalid", b"null", b"[]", b'{"task":{},"error":{},"queue":null}']
    redis = ListRedis([*malformed, entry("RHEL-1", "triage_queue")])

    assert await clear_resolved_errors(redis, ["RHEL-1"], "triage_queue") == 1
    assert redis.entries == malformed
