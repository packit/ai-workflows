import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import requests
from flexmock import flexmock

import ymir.jira_issue_fetcher.jira_issue_fetcher as jira_issue_fetcher_impl
from ymir.common.constants import JIRA_SEARCH_PATH, JiraLabels, RedisQueues
from ymir.common.models import (
    BackportData,
    ClarificationNeededData,
    ErrorData,
    OpenEndedAnalysisData,
    RebaseData,
    Task,
)
from ymir.jira_issue_fetcher.jira_issue_fetcher import JiraIssueFetcher


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Mock environment variables."""
    monkeypatch.setenv("JIRA_URL", "https://jira.test.com")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "test_token")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("QUERY", 'filter = "Jotnar_1000_packages"')


@pytest.fixture
def fetcher(mock_env_vars):
    """Create a JiraIssueFetcher instance with mocked environment."""
    return JiraIssueFetcher()


@pytest.fixture
def mock_redis_context():
    """Create a mock Redis context manager for testing."""
    # Create mock Redis object
    mock_redis = flexmock()

    @asynccontextmanager
    async def mock_context_manager(*_, **__):
        yield mock_redis

    # Mock the redis_client function in the jira_issue_fetcher module where it's imported
    flexmock(jira_issue_fetcher_impl).should_receive("redis_client").replace_with(mock_context_manager)
    return mock_redis, mock_context_manager


def create_async_mock_return_value(value):
    """Create a mock awaitable that returns the given value when awaited."""

    async def async_return():
        return value

    return async_return()


def test_init(mock_env_vars):
    """Test JiraIssueFetcher initialization."""
    fetcher = JiraIssueFetcher()

    assert fetcher.jira_url == "https://jira.test.com"
    assert fetcher.redis_url == "redis://localhost:6379"
    assert fetcher.query == 'filter = "Jotnar_1000_packages"'
    assert fetcher.max_results_per_page == 500
    assert fetcher.headers["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_rate_limit(fetcher):
    """Test rate limiting functionality."""
    flexmock(time).should_receive("time").and_return(0.0, 0.2).one_by_one()

    # Mock asyncio.sleep to return an awaitable coroutine
    async def mock_sleep(sleep_time):
        pass

    flexmock(asyncio).should_receive("sleep").and_return(mock_sleep(0.2)).once()

    fetcher.last_request_time = 0.0
    await fetcher._rate_limit()

    # Should have updated last_request_time
    assert fetcher.last_request_time == 0.2


def test_make_request_with_retries_success(fetcher):
    """Test successful HTTP request."""
    mock_response = flexmock()
    mock_response.should_receive("raise_for_status").once()
    mock_response.should_receive("json").and_return({"issues": []}).once()
    mock_response.status_code = 200

    flexmock(requests).should_receive("post").with_args(
        f"https://jira.test.com/{JIRA_SEARCH_PATH}",
        json={"jql": "test query", "startAt": 0, "maxResults": 50},
        headers=fetcher.headers,
        timeout=90,
    ).and_return(mock_response).once()

    result = fetcher._make_request_with_retries(
        f"https://jira.test.com/{JIRA_SEARCH_PATH}",
        {"jql": "test query", "startAt": 0, "maxResults": 50},
    )

    assert result == {"issues": []}


def test_make_request_with_retries_rate_limited(fetcher):
    """Test HTTP request with rate limiting (429 error)."""
    mock_response = flexmock()
    mock_response.status_code = 429

    flexmock(requests).should_receive("post").and_return(mock_response)
    flexmock(requests.HTTPError)

    # Mock the logger that's defined in the jira_issue_fetcher module
    mock_logger = flexmock()
    mock_logger.should_receive("warning")
    flexmock(sys.modules["ymir.jira_issue_fetcher.jira_issue_fetcher"]).should_receive("logger").and_return(
        mock_logger
    )

    with pytest.raises(requests.HTTPError):
        fetcher._make_request_with_retries(f"https://jira.test.com/{JIRA_SEARCH_PATH}", {"jql": "test query"})


@pytest.mark.asyncio
async def test_search_issues_single_page(fetcher):
    """Test searching issues with single page result."""
    mock_issues = [
        {"key": "TEST-1", "fields": {"labels": []}},
        {"key": "TEST-2", "fields": {"labels": [JiraLabels.RETRY_NEEDED.value]}},
    ]

    # Mock _rate_limit to return an awaitable coroutine
    async def mock_rate_limit():
        pass

    flexmock(fetcher).should_receive("_rate_limit").and_return(mock_rate_limit()).once()
    flexmock(fetcher).should_receive("_make_request_with_retries").with_args(
        f"https://jira.test.com/{JIRA_SEARCH_PATH}",
        json_data={
            "jql": 'filter = "Jotnar_1000_packages"',
            "maxResults": 500,
            "fields": ["key", "labels", "components", "customfield_10669", "fixVersions", "updated"],
        },
    ).and_return(
        {
            "issues": mock_issues,
            "total": 2,
        }
    ).once()

    result = await fetcher.search_issues()

    assert len(result) == 2
    assert result[0]["key"] == "TEST-1"
    assert result[1]["key"] == "TEST-2"


@pytest.mark.asyncio
async def test_search_issues_multiple_pages(fetcher):
    """Test searching issues with pagination."""
    mock_issues_page1 = [{"key": "TEST-1", "fields": {"labels": []}}]
    mock_issues_page2 = [{"key": "TEST-2", "fields": {"labels": []}}]

    # Mock _rate_limit to return an awaitable coroutine, we can't reuse an awaitable coroutine
    async def mock_rate_limit_1():
        pass

    async def mock_rate_limit_2():
        pass

    flexmock(fetcher).should_receive("_rate_limit").and_return(mock_rate_limit_1()).and_return(
        mock_rate_limit_2()
    )
    flexmock(fetcher).should_receive("_make_request_with_retries").and_return(
        {
            "issues": mock_issues_page1,
            "total": 2,
            "nextPageToken": "page2token",
        }
    ).and_return(
        {
            "issues": mock_issues_page2,
            "total": 2,
        }
    )

    # Override max results for this test
    fetcher.max_results_per_page = 1

    result = await fetcher.search_issues()

    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_existing_issue_keys(fetcher, mock_redis_context):
    """Test getting existing issue keys from Redis queues.

    Both triage_queue and triage_queue_todo must contribute to the
    dedup set — otherwise priority-queue items would be invisible and
    the fetcher would re-push them on every sweep.
    """
    # Create actual Task and TriageInputSchema instances
    triage_task_json = json.dumps({"metadata": {"issue": "EXISTING-1"}, "attempts": 0})
    todo_task_json = json.dumps(
        {"metadata": {"issue": "EXISTING-TODO-1"}, "attempts": 0, "user_triggered": True}
    )

    mock_redis, _ = mock_redis_context
    # Dynamically mock every queue so the test stays robust against future
    # additions to RedisQueues. The two triage queues return seeded tasks;
    # everything else is empty.
    for queue in RedisQueues.all_queues():
        if queue == RedisQueues.TRIAGE_QUEUE.value:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([triage_task_json])
            )
        elif queue == RedisQueues.TRIAGE_QUEUE_TODO.value:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([todo_task_json])
            )
        else:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([])
            )

    result = await fetcher._get_existing_issue_keys(mock_redis)

    assert "EXISTING-1" in result
    assert "EXISTING-TODO-1" in result


@pytest.mark.asyncio
async def test_push_issues_to_queue(fetcher, mock_redis_context):
    """Test pushing new issues to the triage queue."""
    mock_redis, _ = mock_redis_context
    # Create a real task and get its JSON representation
    task = Task.from_issue("NEW-1")
    task_json = task.to_json()

    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task_json).and_return(
        create_async_mock_return_value(1)
    ).once()

    issues = [{"key": "NEW-1", "fields": {"labels": []}}]
    existing_keys = set()

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(existing_keys)
    )

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1


@pytest.mark.asyncio
async def test_push_issues_to_queue_skip_existing(fetcher, mock_redis_context):
    """Test that existing issues are skipped."""
    mock_redis, _ = mock_redis_context

    issues = [
        {"key": "EXISTING-1", "fields": {"labels": []}},
        {"key": "NEW-1", "fields": {"labels": []}},
    ]
    existing_keys = {"EXISTING-1"}

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(existing_keys)
    )

    # Create a real task and get its JSON representation
    task = Task.from_issue("NEW-1")
    task_json = task.to_json()

    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task_json).and_return(
        create_async_mock_return_value(1)
    ).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1  # Only NEW-1 should be pushed


@pytest.mark.asyncio
async def test_push_issues_to_queue_skip_labeled_issues(fetcher, mock_redis_context):
    """Test that issues with ymir labels (except retry_needed) are skipped."""
    mock_redis, _ = mock_redis_context

    issues = [
        {"key": "LABELED-1", "fields": {"labels": [JiraLabels.TRIAGED_REBASE.value]}},
        {"key": "RETRY-1", "fields": {"labels": [JiraLabels.RETRY_NEEDED.value]}},
        {"key": "CLEAN-1", "fields": {"labels": []}},
    ]
    existing_keys = set()

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(existing_keys)
    )

    # RETRY-1 must have its trigger label flipped atomically before enqueue.
    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "RETRY-1",
        add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
        remove=[JiraLabels.RETRY_NEEDED.value],
    ).once()

    # Create real tasks and get their JSON representations
    task1 = Task.from_issue("RETRY-1")
    task2 = Task.from_issue("CLEAN-1")
    task1_json = task1.to_json()
    task2_json = task2.to_json()

    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task1_json).and_return(
        create_async_mock_return_value(1)
    ).once()
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task2_json).and_return(
        create_async_mock_return_value(1)
    ).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 2  # RETRY-1 and CLEAN-1 should be pushed


@pytest.mark.asyncio
async def test_push_issues_to_queue_skip_modular(fetcher, mock_redis_context):
    """Test that modular issues are skipped."""
    mock_redis, _ = mock_redis_context

    issues = [
        {"key": "MOD-1", "fields": {"labels": [], "customfield_10669": "perl:5.32/perl-IO-Socket-SSL"}},
        {"key": "MOD-2", "fields": {"labels": [], "customfield_10669": "nodejs:18/nodejs"}},
        {"key": "REG-1", "fields": {"labels": [], "customfield_10669": "regular-component"}},
        {"key": "REG-2", "fields": {"labels": [], "customfield_10669": None}},
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    task1 = Task.from_issue("REG-1")
    task2 = Task.from_issue("REG-2")
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task1.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task2.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 2


@pytest.mark.asyncio
async def test_push_issues_to_queue_skip_ignored_components(fetcher, mock_redis_context):
    """Test that issues with ignored components are skipped."""
    mock_redis, _ = mock_redis_context

    fetcher.ignored_components = {"kernel", "glibc"}

    issues = [
        {"key": "IGN-1", "fields": {"labels": [], "components": [{"name": "kernel"}]}},
        {"key": "IGN-2", "fields": {"labels": [], "components": [{"name": "glibc"}]}},
        {"key": "OK-1", "fields": {"labels": [], "components": [{"name": "bash"}]}},
        {"key": "OK-2", "fields": {"labels": [], "components": []}},
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    task1 = Task.from_issue("OK-1")
    task2 = Task.from_issue("OK-2")
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task1.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task2.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 2


@pytest.mark.asyncio
async def test_push_issues_to_queue_max_issues(fetcher, mock_redis_context):
    """Test that MAX_ISSUES limits the number of enqueued issues."""
    mock_redis, _ = mock_redis_context

    fetcher.max_issues = 2

    issues = [
        {"key": "ISS-1", "fields": {"labels": []}},
        {"key": "ISS-2", "fields": {"labels": []}},
        {"key": "ISS-3", "fields": {"labels": []}},
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    task1 = Task.from_issue("ISS-1")
    task2 = Task.from_issue("ISS-2")
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task1.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task2.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 2


@pytest.mark.asyncio
async def test_push_issues_to_queue_max_issues_excludes_filtered(fetcher, mock_redis_context):
    """Test that filtered issues don't count towards MAX_ISSUES."""
    mock_redis, _ = mock_redis_context

    fetcher.max_issues = 2

    issues = [
        {"key": "ISS-1", "fields": {"labels": []}},
        {"key": "MOD-1", "fields": {"labels": [], "customfield_10669": "perl:5.32/perl-IO"}},
        {"key": "ISS-2", "fields": {"labels": []}},
        {"key": "ISS-3", "fields": {"labels": []}},
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    task1 = Task.from_issue("ISS-1")
    task2 = Task.from_issue("ISS-2")
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task1.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task2.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 2


@pytest.mark.asyncio
async def test_push_retry_needed_issue(fetcher, mock_redis_context):
    """ymir_retry_needed: flip the label atomically, enqueue without user_triggered."""
    mock_redis, _ = mock_redis_context

    issues = [{"key": "RETRY-1", "fields": {"labels": [JiraLabels.RETRY_NEEDED.value]}}]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "RETRY-1",
        add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
        remove=[JiraLabels.RETRY_NEEDED.value],
    ).once()

    # ymir_retry_needed can be set by either a maintainer or an agent retrying a
    # failed run, so user_triggered stays False here — maintainers who want the
    # user-triggered treatment (ack comment, comments on results) use ymir_todo.
    expected_task = Task.from_issue("RETRY-1", user_triggered=False)
    mock_redis.should_receive("lpush").with_args(
        RedisQueues.TRIAGE_QUEUE.value, expected_task.to_json()
    ).and_return(create_async_mock_return_value(1)).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1


@pytest.mark.asyncio
async def test_skip_retry_needed_when_in_progress(fetcher, mock_redis_context):
    """ymir_retry_needed + an in-progress label: do not enqueue, do not flip labels."""
    mock_redis, _ = mock_redis_context

    issues = [
        {
            "key": "RETRY-INPROG-1",
            "fields": {"labels": [JiraLabels.RETRY_NEEDED.value, JiraLabels.TRIAGE_IN_PROGRESS.value]},
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    flexmock(fetcher).should_receive("_edit_jira_labels").never()
    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


@pytest.mark.asyncio
async def test_retry_needed_skip_when_label_flip_fails(fetcher, mock_redis_context):
    """If the retry-needed flip raises, skip the Redis push entirely."""
    mock_redis, _ = mock_redis_context

    issues = [{"key": "RETRY-FAIL", "fields": {"labels": [JiraLabels.RETRY_NEEDED.value]}}]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    flexmock(fetcher).should_receive("_edit_jira_labels").and_raise(
        requests.HTTPError("Jira write failed")
    ).once()

    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


@pytest.mark.parametrize(
    ("trigger_label", "user_triggered", "issue_key", "expected_queue"),
    [
        (JiraLabels.TODO.value, True, "TODO-DRY", RedisQueues.TRIAGE_QUEUE_TODO.value),
        (JiraLabels.RETRY_NEEDED.value, False, "RETRY-DRY", RedisQueues.TRIAGE_QUEUE.value),
    ],
)
@pytest.mark.asyncio
async def test_dry_run_skips_flip_but_still_pushes(
    monkeypatch,
    mock_env_vars,
    mock_redis_context,
    trigger_label,
    user_triggered,
    issue_key,
    expected_queue,
):
    """DRY_RUN=true: skip the Jira atomic flip for trigger labels, but still push to Redis.

    The pushed Task preserves user_triggered so the agent (also presumably in
    DRY_RUN) sees the same dry-mode flow as it would for a real trigger. The
    queue selection still respects priority: ymir_todo tasks go to
    triage_queue_todo, retry/normal tasks to triage_queue.
    """
    monkeypatch.setenv("DRY_RUN", "true")
    fetcher = JiraIssueFetcher()
    mock_redis, _ = mock_redis_context

    issues = [{"key": issue_key, "fields": {"labels": [trigger_label]}}]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    # For ymir_todo issues the fetcher verifies the label-add author; we don't
    # exercise that path here so unconditionally pass.
    flexmock(fetcher).should_receive("_label_added_by_rh_employee").and_return(True)
    # Must NOT touch Jira in dry-run mode.
    flexmock(fetcher).should_receive("_edit_jira_labels").never()

    expected_task = Task.from_issue(issue_key, user_triggered=user_triggered)
    mock_redis.should_receive("lpush").with_args(expected_queue, expected_task.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1


@pytest.mark.asyncio
async def test_run_full_workflow(fetcher):
    """Test the complete run workflow."""
    mock_issues = [{"key": "TEST-1", "fields": {"labels": []}}]

    # Mock all the methods
    flexmock(fetcher).should_receive("search_issues").and_return(
        create_async_mock_return_value(mock_issues)
    ).once()
    flexmock(fetcher).should_receive("push_issues_to_queue").with_args(mock_issues).and_return(
        create_async_mock_return_value(1)
    ).once()
    flexmock(fetcher).should_receive("_process_consolidation_labels").with_args(mock_issues).and_return(
        create_async_mock_return_value(0)
    ).once()

    await fetcher.run()


@pytest.mark.asyncio
async def test_run_full_workflow_with_labeled_issues(fetcher, mock_redis_context):
    """Test the complete run workflow with issues that have different label states."""
    mock_redis, _ = mock_redis_context

    # Create test issues with different label states. All of these are
    # *already known to Redis* (added to lists/queues below), so the
    # fetcher's dedup must skip them — except for the explicit retry trigger,
    # which overrides the skip.
    mock_issues = [
        {
            "key": "ISSUE-1",
            "fields": {"labels": []},
        },  # No labels but already in OPEN_ENDED_ANALYSIS_LIST - should be skipped
        {
            "key": "ISSUE-2",
            "fields": {"labels": ["ymir_rebase_in_progress"]},
        },  # Has ymir label - should be skipped
        {
            "key": "ISSUE-3",
            "fields": {"labels": ["ymir_backport_in_progress"]},
        },  # Has ymir label - should be skipped
        {
            "key": "ISSUE-4",
            "fields": {"labels": ["ymir_retry_needed"]},
        },  # Has retry label - should be pushed (retry overrides 'already known')
        {
            "key": "ISSUE-5",
            "fields": {"labels": []},
        },  # No labels but already in CLARIFICATION_NEEDED_QUEUE - should be skipped
        {
            "key": "ISSUE-6",
            "fields": {"labels": ["ymir_completed"]},
        },  # Has ymir label - should be skipped
    ]

    # Create existing issues that are already in Redis queues using the correct data structures
    # Input queues (REBASE_QUEUE, BACKPORT_QUEUE) contain Task objects with triage_agent.State metadata
    # Data queues contain the appropriate schema objects directly

    # Create Task objects for input queues with proper triage_agent.State metadata
    triage_state_for_rebase = {
        "jira_issue": "ISSUE-2",
        "triage_result": {
            "resolution": "rebase",
            "data": RebaseData(
                jira_issue="ISSUE-2",
                package="test-package",
                version="1.0.0",
                justification="Update to latest upstream version",
            ).model_dump(),
        },
    }
    task_for_rebase = Task(metadata=triage_state_for_rebase).model_dump_json()

    triage_state_for_backport = {
        "jira_issue": "ISSUE-3",
        "triage_result": {
            "resolution": "backport",
            "data": BackportData(
                jira_issue="ISSUE-3",
                package="test-package",
                patch_urls=["https://example.com/patch"],
                justification="Security fix",
                cve_id="CVE-2023-1234",
            ).model_dump(),
        },
    }
    task_for_backport = Task(metadata=triage_state_for_backport).model_dump_json()

    # Create schema objects for data queues
    existing_issues = {
        "ISSUE-1": OpenEndedAnalysisData(
            jira_issue="ISSUE-1",
            summary="Issue requires no action",
            recommendation="No action needed.",
        ).model_dump_json(),
        "ISSUE-2": task_for_rebase,  # Task object for input queue
        "ISSUE-3": task_for_backport,  # Task object for input queue
        "ISSUE-4": OpenEndedAnalysisData(
            jira_issue="ISSUE-4",
            summary="Issue requires no action",
            recommendation="No action needed.",
        ).model_dump_json(),
        "ISSUE-5": Task(
            metadata={
                "jira_issue": "ISSUE-5",
                "triage_result": {
                    "resolution": "clarification-needed",
                    "data": ClarificationNeededData(
                        jira_issue="ISSUE-5",
                        findings="Investigation incomplete",
                        additional_info_needed="More details needed",
                    ).model_dump(),
                },
            }
        ).model_dump_json(),
        "ISSUE-6": ErrorData(
            jira_issue="ISSUE-6", details="Build failed"
        ).model_dump_json(),  # Use ErrorData for error_list
    }

    # Mock lrange calls for existing issues distributed across different queues
    # Distribute issues across different queues to test the logic
    mock_redis.should_receive("lrange").with_args(
        RedisQueues.OPEN_ENDED_ANALYSIS_LIST.value, 0, -1
    ).and_return(create_async_mock_return_value([existing_issues["ISSUE-1"], existing_issues["ISSUE-4"]]))

    mock_redis.should_receive("lrange").with_args(RedisQueues.REBASE_QUEUE_C9S.value, 0, -1).and_return(
        create_async_mock_return_value([existing_issues["ISSUE-2"]])
    )

    mock_redis.should_receive("lrange").with_args(RedisQueues.BACKPORT_QUEUE_C9S.value, 0, -1).and_return(
        create_async_mock_return_value([existing_issues["ISSUE-3"]])
    )

    mock_redis.should_receive("lrange").with_args(
        RedisQueues.CLARIFICATION_NEEDED_QUEUE.value, 0, -1
    ).and_return(create_async_mock_return_value([existing_issues["ISSUE-5"]]))

    mock_redis.should_receive("lrange").with_args(RedisQueues.ERROR_LIST.value, 0, -1).and_return(
        create_async_mock_return_value([existing_issues["ISSUE-6"]])
    )

    # Mock every other queue as empty so the test stays robust against
    # future additions to RedisQueues (e.g. the _todo and rebuild queues).
    seeded_queues = {
        RedisQueues.OPEN_ENDED_ANALYSIS_LIST.value,
        RedisQueues.REBASE_QUEUE_C9S.value,
        RedisQueues.BACKPORT_QUEUE_C9S.value,
        RedisQueues.CLARIFICATION_NEEDED_QUEUE.value,
        RedisQueues.ERROR_LIST.value,
    }
    for queue in RedisQueues.all_queues():
        if queue not in seeded_queues:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([])
            )

    # ISSUE-4 has ymir_retry_needed → atomic label flip before enqueue.
    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "ISSUE-4",
        add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
        remove=[JiraLabels.RETRY_NEEDED.value],
    ).once()

    # Only ISSUE-4 should be pushed: ymir_retry_needed explicitly overrides
    # the "already known to Redis" skip. The two no-label issues
    # (ISSUE-1, ISSUE-5) are already tracked in Redis lists, so the fetcher
    # leaves them alone instead of double-pushing. The ymir-labelled issues
    # (ISSUE-2, ISSUE-3, ISSUE-6) are skipped because they have terminal
    # markers indicating processing is already happening or done.
    task4 = Task.from_issue("ISSUE-4")
    mock_redis.should_receive("lpush").with_args(RedisQueues.TRIAGE_QUEUE.value, task4.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()

    # Mock the methods that are called internally
    flexmock(fetcher).should_receive("search_issues").and_return(
        create_async_mock_return_value(mock_issues)
    ).once()

    # Run the workflow
    await fetcher.run()


@pytest.mark.parametrize(
    ("labels", "user_triggered", "expected_queue", "label_to_remove"),
    [
        # Fresh issue with no labels → normal queue
        ([], False, RedisQueues.TRIAGE_QUEUE.value, None),
        # ymir_todo trigger → priority queue
        ([JiraLabels.TODO.value], True, RedisQueues.TRIAGE_QUEUE_TODO.value, JiraLabels.TODO.value),
        # ymir_retry_needed trigger → normal queue (not priority)
        (
            [JiraLabels.RETRY_NEEDED.value],
            False,
            RedisQueues.TRIAGE_QUEUE.value,
            JiraLabels.RETRY_NEEDED.value,
        ),
    ],
)
@pytest.mark.asyncio
async def test_push_routes_to_priority_queue_for_user_triggered(
    fetcher, mock_redis_context, labels, user_triggered, expected_queue, label_to_remove
):
    """Queue routing: ymir_todo → triage_queue_todo (priority); others → triage_queue."""
    mock_redis, _ = mock_redis_context
    issue_key = "ROUTE-1"

    issues = [{"key": issue_key, "fields": {"labels": labels}}]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    flexmock(fetcher).should_receive("_label_added_by_rh_employee").and_return(True)
    if label_to_remove:
        flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
            issue_key,
            add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
            remove=[label_to_remove],
        ).once()
    else:
        flexmock(fetcher).should_receive("_edit_jira_labels").never()

    expected_task = Task.from_issue(issue_key, user_triggered=user_triggered)
    mock_redis.should_receive("lpush").with_args(expected_queue, expected_task.to_json()).and_return(
        create_async_mock_return_value(1)
    ).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1


@pytest.mark.asyncio
async def test_push_user_triggered_issue(fetcher, mock_redis_context):
    """ymir_todo on an otherwise clean issue: enqueue as user_triggered, flip the label."""
    mock_redis, _ = mock_redis_context

    issues = [{"key": "TODO-1", "fields": {"labels": [JiraLabels.TODO.value]}}]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    # Author verification passes — label was added by a Red Hat Employee.
    flexmock(fetcher).should_receive("_label_added_by_rh_employee").with_args("TODO-1").and_return(
        True
    ).once()

    # The critical label flip must be invoked before the Redis push.
    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "TODO-1",
        add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
        remove=[JiraLabels.TODO.value],
    ).once()

    expected_task = Task.from_issue("TODO-1", user_triggered=True)
    # ymir_todo-triggered tasks go to the priority queue so they jump ahead of
    # normal-flow tasks in the triage agent's BRPOP order.
    mock_redis.should_receive("lpush").with_args(
        RedisQueues.TRIAGE_QUEUE_TODO.value, expected_task.to_json()
    ).and_return(create_async_mock_return_value(1)).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1


@pytest.mark.asyncio
async def test_skip_user_triggered_when_in_progress(fetcher, mock_redis_context):
    """ymir_todo on an issue already in-progress: do not enqueue, do not flip labels."""
    mock_redis, _ = mock_redis_context

    issues = [
        {
            "key": "TODO-INPROG-1",
            "fields": {"labels": [JiraLabels.TODO.value, JiraLabels.TRIAGE_IN_PROGRESS.value]},
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    # in_progress short-circuits before author verification runs.
    flexmock(fetcher).should_receive("_label_added_by_rh_employee").never()
    # _edit_jira_labels must NOT be called for an in-progress issue.
    flexmock(fetcher).should_receive("_edit_jira_labels").never()
    # lpush must NOT be called either.
    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


@pytest.mark.asyncio
async def test_skip_user_triggered_when_not_rh_employee(fetcher, mock_redis_context):
    """ymir_todo added by a non-RH user: skip the issue, remove the bogus label,
    do not push."""
    mock_redis, _ = mock_redis_context

    issues = [{"key": "TODO-EXT-1", "fields": {"labels": [JiraLabels.TODO.value]}}]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    flexmock(fetcher).should_receive("_label_added_by_rh_employee").with_args("TODO-EXT-1").and_return(
        False
    ).once()
    # Bogus label is removed so the per-sweep verification cost doesn't repeat
    # forever. No Redis push.
    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "TODO-EXT-1", add=[], remove=[JiraLabels.TODO.value]
    ).once()
    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


@pytest.mark.asyncio
async def test_user_triggered_skip_when_label_flip_fails(fetcher, mock_redis_context):
    """If the atomic label flip raises after retries, skip the Redis push entirely."""
    mock_redis, _ = mock_redis_context

    issues = [{"key": "TODO-FAIL", "fields": {"labels": [JiraLabels.TODO.value]}}]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    flexmock(fetcher).should_receive("_label_added_by_rh_employee").with_args("TODO-FAIL").and_return(
        True
    ).once()

    flexmock(fetcher).should_receive("_edit_jira_labels").and_raise(
        requests.HTTPError("Jira write failed")
    ).once()

    # No push must occur — pushing without the in-progress marker would cause
    # the next sweep to re-enqueue the same issue.
    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


def _changelog_response(histories, is_last_page=True):
    """Build a fake requests.get response for the paginated /changelog endpoint."""
    mock = flexmock(status_code=200)
    mock.should_receive("raise_for_status")
    mock.should_receive("json").and_return({"values": histories, "isLastPage": is_last_page})
    return mock


def _user_response(groups):
    """Build a fake requests.get response for the user-with-groups endpoint."""
    mock = flexmock(status_code=200)
    mock.should_receive("raise_for_status")
    mock.should_receive("json").and_return({"groups": {"items": [{"name": g} for g in groups]}})
    return mock


def test_label_added_by_rh_employee_true(fetcher):
    """Latest ymir_todo add was performed by a member of the Red Hat Employee group."""
    histories = [
        {
            "created": "2026-05-25T13:54:47.861+0000",
            "author": {"accountId": "rh-user-1"},
            "items": [{"field": "labels", "fromString": "", "toString": "ymir_todo"}],
        }
    ]
    flexmock(requests).should_receive("get").and_return(
        _changelog_response(histories),
        _user_response(["Red Hat Employee", "confluence-users"]),
    ).one_by_one()

    assert fetcher._label_added_by_rh_employee("RHEL-1") is True


def test_label_added_by_rh_employee_false_when_author_not_in_group(fetcher):
    """Latest ymir_todo add was performed by a user outside the Red Hat Employee group."""
    histories = [
        {
            "created": "2026-05-25T13:54:47.861+0000",
            "author": {"accountId": "external-1"},
            "items": [{"field": "labels", "fromString": "", "toString": "ymir_todo"}],
        }
    ]
    flexmock(requests).should_receive("get").and_return(
        _changelog_response(histories),
        _user_response(["confluence-users"]),
    ).one_by_one()

    assert fetcher._label_added_by_rh_employee("RHEL-2") is False


def test_label_added_by_rh_employee_picks_latest_add(fetcher):
    """If ymir_todo was added by an RH user then removed and re-added by an external
    user, the external user (latest add) wins and the helper returns False."""
    histories = [
        {
            "created": "2026-05-20T10:00:00.000+0000",
            "author": {"accountId": "rh-user-1"},
            "items": [{"field": "labels", "fromString": "", "toString": "ymir_todo"}],
        },
        {
            "created": "2026-05-21T10:00:00.000+0000",
            "author": {"accountId": "rh-user-1"},
            "items": [{"field": "labels", "fromString": "ymir_todo", "toString": ""}],
        },
        {
            "created": "2026-05-22T10:00:00.000+0000",
            "author": {"accountId": "external-1"},
            "items": [{"field": "labels", "fromString": "", "toString": "ymir_todo"}],
        },
    ]
    flexmock(requests).should_receive("get").and_return(
        _changelog_response(histories),
        _user_response(["confluence-users"]),
    ).one_by_one()

    assert fetcher._label_added_by_rh_employee("RHEL-3") is False


def test_label_added_by_rh_employee_false_when_no_add_event(fetcher):
    """No changelog entry adds ymir_todo (label predates the changelog or was
    written via a path Jira does not record): treat as non-RH-employee."""
    histories = [
        {
            "created": "2026-05-25T13:54:47.861+0000",
            "author": {"accountId": "rh-user-1"},
            "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}],
        }
    ]
    flexmock(requests).should_receive("get").and_return(_changelog_response(histories)).once()

    assert fetcher._label_added_by_rh_employee("RHEL-4") is False


# ============================================================================
# _is_label_stale / _find_stale_in_flight_label
# ============================================================================

_STALE_UPDATED = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
_FRESH_UPDATED = datetime.now(UTC).isoformat()


@pytest.mark.parametrize(
    ("updated", "expected"),
    [
        (_STALE_UPDATED, True),
        (_FRESH_UPDATED, False),
        # Real Jira format: no colon in the UTC offset.
        ("2020-01-01T00:00:00.000+0000", True),
        (None, False),  # missing field fails closed
        ("not-a-timestamp", False),  # unparseable fails closed
    ],
)
def test_is_label_stale(updated, expected):
    issue = {"fields": {"updated": updated}} if updated is not None else {"fields": {}}
    assert JiraIssueFetcher._is_label_stale(issue, threshold_hours=24) is expected


def test_is_label_stale_naive_datetime_treated_as_utc():
    """A timestamp with no timezone info is treated as UTC rather than raising."""
    naive_but_old = (datetime.now(UTC) - timedelta(hours=100)).replace(tzinfo=None).isoformat()
    issue = {"fields": {"updated": naive_but_old}}
    assert JiraIssueFetcher._is_label_stale(issue, threshold_hours=24) is True


def test_find_stale_in_flight_label_returns_label_when_alone_and_stale(fetcher):
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(issue, [JiraLabels.TRIAGE_IN_PROGRESS.value])
    assert result == JiraLabels.TRIAGE_IN_PROGRESS.value


def test_find_stale_in_flight_label_none_when_fresh(fetcher):
    issue = {"fields": {"updated": _FRESH_UPDATED}}
    result = fetcher._find_stale_in_flight_label(issue, [JiraLabels.TRIAGE_IN_PROGRESS.value])
    assert result is None


def test_find_stale_in_flight_label_none_when_no_in_flight_label(fetcher):
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(issue, [JiraLabels.RETRY_NEEDED.value])
    assert result is None


def test_find_stale_in_flight_label_none_when_terminal_label_coexists(fetcher):
    """A terminal outcome label alongside the in-flight one means it's not
    actually stuck — just not cleaned up, or the two are unrelated."""
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(
        issue, [JiraLabels.TRIAGED_BACKPORT.value, JiraLabels.BACKPORTED.value]
    )
    assert result is None


def test_find_stale_in_flight_label_ignores_retry_needed_and_todo(fetcher):
    """ymir_retry_needed / ymir_todo are trigger labels, not outcomes — their
    presence alongside a stale in-flight label doesn't block detection."""
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(
        issue,
        [JiraLabels.TRIAGE_IN_PROGRESS.value, JiraLabels.RETRY_NEEDED.value, JiraLabels.TODO.value],
    )
    assert result == JiraLabels.TRIAGE_IN_PROGRESS.value


def test_find_stale_in_flight_label_downstream_stage(fetcher):
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(issue, [JiraLabels.TRIAGED_REBUILD.value])
    assert result == JiraLabels.TRIAGED_REBUILD.value


# ============================================================================
# push_issues_to_queue: stale in-flight label safety net
# ============================================================================


@pytest.mark.asyncio
async def test_push_stale_triage_in_progress_reenqueued(fetcher, mock_redis_context):
    """Stale ymir_triage_in_progress with no other Ymir label: flip to
    ymir_retry_needed, then let the existing retry machinery re-triage it
    (non-user-triggered, since the original trigger context is lost)."""
    mock_redis, _ = mock_redis_context

    issues = [
        {
            "key": "STUCK-1",
            "fields": {"labels": [JiraLabels.TRIAGE_IN_PROGRESS.value], "updated": _STALE_UPDATED},
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    # First flip: the stale-detection safety net itself.
    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "STUCK-1",
        add=[JiraLabels.RETRY_NEEDED.value],
        remove=[JiraLabels.TRIAGE_IN_PROGRESS.value],
    ).once()
    # Second flip: the pre-existing retry_needed handling consumes the
    # trigger it just created, same as for a maintainer-set ymir_retry_needed.
    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "STUCK-1",
        add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
        remove=[JiraLabels.RETRY_NEEDED.value],
    ).once()

    expected_task = Task.from_issue("STUCK-1", user_triggered=False)
    mock_redis.should_receive("lpush").with_args(
        RedisQueues.TRIAGE_QUEUE.value, expected_task.to_json()
    ).and_return(create_async_mock_return_value(1)).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1


@pytest.mark.asyncio
async def test_push_stale_triaged_backport_reenqueued(fetcher, mock_redis_context):
    """Stale ymir_triaged_backport with no terminal outcome label: same
    flip-to-retry-needed recovery as the triage-stage case."""
    mock_redis, _ = mock_redis_context

    issues = [
        {
            "key": "STUCK-2",
            "fields": {"labels": [JiraLabels.TRIAGED_BACKPORT.value], "updated": _STALE_UPDATED},
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )

    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "STUCK-2",
        add=[JiraLabels.RETRY_NEEDED.value],
        remove=[JiraLabels.TRIAGED_BACKPORT.value],
    ).once()
    flexmock(fetcher).should_receive("_edit_jira_labels").with_args(
        "STUCK-2",
        add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
        remove=[JiraLabels.RETRY_NEEDED.value],
    ).once()

    expected_task = Task.from_issue("STUCK-2", user_triggered=False)
    mock_redis.should_receive("lpush").with_args(
        RedisQueues.TRIAGE_QUEUE.value, expected_task.to_json()
    ).and_return(create_async_mock_return_value(1)).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1


@pytest.mark.asyncio
async def test_push_stale_label_skipped_when_still_queued_in_redis(fetcher, mock_redis_context):
    """Regression test: a stale-looking in-flight label whose task is still
    genuinely queued in Redis (e.g. the SIGTERM handler already re-pushed
    it, or a downstream queue is simply backed up) must NOT be flipped to
    ymir_retry_needed and re-enqueued - doing so would bypass the
    existing_keys dedup guard and start a second, concurrent agent run on
    top of the one already queued."""
    mock_redis, _ = mock_redis_context

    issues = [
        {
            "key": "STUCK-QUEUED",
            "fields": {"labels": [JiraLabels.TRIAGED_BACKPORT.value], "updated": _STALE_UPDATED},
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value({"STUCK-QUEUED"})
    )
    flexmock(fetcher).should_receive("_edit_jira_labels").never()
    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


@pytest.mark.asyncio
async def test_push_fresh_triage_in_progress_still_skipped(fetcher, mock_redis_context):
    """Non-stale ymir_triage_in_progress: unchanged pre-existing behavior —
    skip and don't touch Jira or Redis."""
    mock_redis, _ = mock_redis_context

    issues = [
        {
            "key": "FRESH-1",
            "fields": {"labels": [JiraLabels.TRIAGE_IN_PROGRESS.value], "updated": _FRESH_UPDATED},
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    flexmock(fetcher).should_receive("_edit_jira_labels").never()
    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


@pytest.mark.asyncio
async def test_push_stale_in_progress_with_terminal_label_still_skipped(fetcher, mock_redis_context):
    """Stale ymir_triaged_rebase alongside its terminal outcome label
    (ymir_rebased) means processing actually finished — the in-flight label
    just wasn't cleaned up. Not a stuck task: leave it alone."""
    mock_redis, _ = mock_redis_context

    issues = [
        {
            "key": "DONE-1",
            "fields": {
                "labels": [JiraLabels.TRIAGED_REBASE.value, JiraLabels.REBASED.value],
                "updated": _STALE_UPDATED,
            },
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    flexmock(fetcher).should_receive("_edit_jira_labels").never()
    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


@pytest.mark.asyncio
async def test_push_stale_reenqueue_skips_when_flip_fails(fetcher, mock_redis_context):
    """If the stale-recovery flip itself raises, skip the Redis push —
    otherwise the next sweep would find no trigger label at all."""
    mock_redis, _ = mock_redis_context

    issues = [
        {
            "key": "STUCK-FAIL",
            "fields": {"labels": [JiraLabels.TRIAGE_IN_PROGRESS.value], "updated": _STALE_UPDATED},
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    flexmock(fetcher).should_receive("_edit_jira_labels").and_raise(
        requests.HTTPError("Jira write failed")
    ).once()
    mock_redis.should_receive("lpush").never()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 0


@pytest.mark.asyncio
async def test_push_stale_reenqueue_dry_run_skips_flip_but_still_pushes(monkeypatch, mock_env_vars):
    """DRY_RUN=true: skip both Jira label flips, but still reconstruct and
    push the task, matching the existing dry-run contract for other trigger
    paths (ymir_todo, ymir_retry_needed)."""
    monkeypatch.setenv("DRY_RUN", "true")
    fetcher = JiraIssueFetcher()

    mock_redis = flexmock()

    @asynccontextmanager
    async def mock_context_manager(*_, **__):
        yield mock_redis

    flexmock(jira_issue_fetcher_impl).should_receive("redis_client").replace_with(mock_context_manager)

    issues = [
        {
            "key": "STUCK-DRY",
            "fields": {"labels": [JiraLabels.TRIAGE_IN_PROGRESS.value], "updated": _STALE_UPDATED},
        }
    ]

    flexmock(fetcher).should_receive("_get_existing_issue_keys").and_return(
        create_async_mock_return_value(set())
    )
    flexmock(fetcher).should_receive("_edit_jira_labels").never()

    expected_task = Task.from_issue("STUCK-DRY", user_triggered=False)
    mock_redis.should_receive("lpush").with_args(
        RedisQueues.TRIAGE_QUEUE.value, expected_task.to_json()
    ).and_return(create_async_mock_return_value(1)).once()

    result = await fetcher.push_issues_to_queue(issues)

    assert result == 1


def test_label_added_by_rh_employee_walks_paginated_changelog(fetcher):
    """The ymir_todo add appears on the second page; helper must paginate to find it."""
    page1 = [
        {
            "created": "2026-04-01T10:00:00.000+0000",
            "author": {"accountId": "rh-user-1"},
            "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}],
        }
    ]
    page2 = [
        {
            "created": "2026-05-25T13:54:47.861+0000",
            "author": {"accountId": "rh-user-1"},
            "items": [{"field": "labels", "fromString": "", "toString": "ymir_todo"}],
        }
    ]
    flexmock(requests).should_receive("get").and_return(
        _changelog_response(page1, is_last_page=False),
        _changelog_response(page2, is_last_page=True),
        _user_response(["Red Hat Employee"]),
    ).one_by_one()

    assert fetcher._label_added_by_rh_employee("RHEL-LONG") is True
