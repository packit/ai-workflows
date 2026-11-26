import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import requests
from flexmock import flexmock

import ymir.jira_issue_fetcher.jira_issue_fetcher as jira_issue_fetcher_impl
from ymir.common.constants import JIRA_SEARCH_PATH, JiraLabels, RedisQueues
from ymir.common.issue_lock import LOCK_KEY_PREFIX
from ymir.common.models import (
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


async def _empty_async_iter(*_args, **_kwargs):
    return
    yield


@pytest.fixture(autouse=True)
def deterministic_task_execution_id():
    """Keep serialized Task expectations stable while exercising queue behavior."""
    field = Task.model_fields["execution_id"]
    default_factory = field.default_factory
    field.default_factory = lambda: UUID(int=0)
    Task.model_rebuild(force=True)
    yield
    field.default_factory = default_factory
    Task.model_rebuild(force=True)


def _lock_keys_async_iter(*issue_keys):
    async def _iter(*_args, **_kwargs):
        for key in issue_keys:
            yield f"{LOCK_KEY_PREFIX}{key}".encode()

    return _iter


@pytest.fixture
def mock_redis_context():
    """Create a mock Redis context manager for testing."""
    # Create mock Redis object
    mock_redis = flexmock()
    mock_redis.should_receive("scan_iter").and_return(_empty_async_iter())

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
async def test_get_existing_issue_keys_includes_reproducer_queues(fetcher, mock_redis_context):
    """Queued reproducer tasks must be visible to the duplicate/stale guard.

    Without a match arm for reproducer_queue / _todo, a stale-looking
    ymir_reproducer_in_progress would be flipped even while work is still
    sitting in Redis.
    """
    reproducer_task_json = json.dumps(
        {"metadata": {"jira_issue": "RHEL-REPRO-1", "package": "bind"}, "attempts": 0}
    )
    reproducer_todo_json = json.dumps(
        {
            "metadata": {"jira_issue": "RHEL-REPRO-TODO", "package": "bind"},
            "attempts": 0,
            "user_triggered": True,
        }
    )

    mock_redis, _ = mock_redis_context
    for queue in RedisQueues.all_queues():
        if queue == RedisQueues.REPRODUCER_QUEUE.value:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([reproducer_task_json])
            )
        elif queue == RedisQueues.REPRODUCER_QUEUE_TODO.value:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([reproducer_todo_json])
            )
        else:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([])
            )

    result = await fetcher._get_existing_issue_keys(mock_redis)

    assert "RHEL-REPRO-1" in result
    assert "RHEL-REPRO-TODO" in result


@pytest.mark.asyncio
async def test_get_existing_issue_keys_handles_error_list_entry_wrapper(fetcher, mock_redis_context):
    """error_list entries pushed as ErrorListEntry (queue/task/error_id wrapper)

    must still be deduped by jira_issue, same as legacy bare-ErrorData entries.
    """
    wrapped_entry_json = json.dumps(
        {
            "error_id": 1,
            "timestamp": "2026-01-01T00:00:00Z",
            "queue": "rebase_queue_c9s",
            "task": {"metadata": {"issue": "RHEL-WRAPPED"}, "attempts": 3, "user_triggered": False},
            "error": {"details": "boom", "jira_issue": "RHEL-WRAPPED"},
        }
    )

    mock_redis, _ = mock_redis_context
    for queue in RedisQueues.all_queues():
        if queue == RedisQueues.ERROR_LIST.value:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([wrapped_entry_json])
            )
        else:
            mock_redis.should_receive("lrange").with_args(queue, 0, -1).and_return(
                create_async_mock_return_value([])
            )

    result = await fetcher._get_existing_issue_keys(mock_redis)

    assert "RHEL-WRAPPED" in result


@pytest.mark.asyncio
async def test_discovery_leaves_queued_downstream_work_and_accepts_explicit_retry(
    fetcher, mock_redis_context
):
    submitted = []

    async def connect():
        return object()

    async def submit(_client, intent):
        submitted.append(intent)
        return "accepted"

    async def existing(_redis):
        return {"RHEL-1", "RHEL-2"}

    flexmock(fetcher).should_receive("_get_existing_issue_keys").replace_with(existing)
    flexmock(jira_issue_fetcher_impl).should_receive("connect_triage_ingress").replace_with(connect)
    flexmock(jira_issue_fetcher_impl).should_receive("submit_triage_intent").replace_with(submit)
    assert (
        await fetcher.submit_temporal_issues(
            [
                {"key": "RHEL-1", "fields": {"labels": []}},
                {"key": "RHEL-2", "fields": {"labels": [JiraLabels.RETRY_NEEDED.value]}},
            ]
        )
        == 1
    )
    [intent] = submitted
    assert intent.issue == "RHEL-2" and intent.retry_label == JiraLabels.RETRY_NEEDED.value


def test_find_stale_in_flight_label_ignores_triage_only_marker(fetcher):
    """The persisted triage-only marker is intent, not an outcome — it must not
    block recovery of a stale in-flight label."""
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(
        issue, [JiraLabels.TRIAGE_IN_PROGRESS.value, JiraLabels.TRIAGE_ONLY.value]
    )
    assert result == JiraLabels.TRIAGE_IN_PROGRESS.value


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


def test_find_stale_in_flight_label_ignores_confidence_label(fetcher):
    """An applicability-confidence label is metadata, not a blocking outcome.

    Confidence is written before the terminal label, so a worker killed in that
    window leaves it beside ymir_triage_in_progress.
    """
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(
        issue,
        [
            JiraLabels.TRIAGE_IN_PROGRESS.value,
            JiraLabels.APPLICABILITY_CONFIDENCE_MEDIUM.value,
        ],
    )
    assert result == JiraLabels.TRIAGE_IN_PROGRESS.value


def test_find_stale_in_flight_label_ignores_confidence_during_backport(fetcher):
    """Confidence coexists with the backport handoff label for as long as the
    downstream task waits in Redis; recovery must still see the stage as stale."""
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(
        issue,
        [
            JiraLabels.TRIAGED_BACKPORT.value,
            JiraLabels.APPLICABILITY_CONFIDENCE_HIGH.value,
        ],
    )
    assert result == JiraLabels.TRIAGED_BACKPORT.value


def test_find_stale_in_flight_label_downstream_stage(fetcher):
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(issue, [JiraLabels.TRIAGED_REBUILD.value])
    assert result == JiraLabels.TRIAGED_REBUILD.value


def test_find_stale_reproducer_in_progress_despite_triage_labels(fetcher):
    """Reproducer coexists with triage outcome labels by design — those must
    not block stale recovery of ymir_reproducer_in_progress."""
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(
        issue,
        [
            JiraLabels.TRIAGED_BACKPORT.value,
            JiraLabels.REPRODUCER_IN_PROGRESS.value,
            JiraLabels.CLI_TRIAGE.value,
        ],
    )
    assert result == JiraLabels.REPRODUCER_IN_PROGRESS.value


def test_find_stale_reproducer_in_progress_blocked_by_terminal_reproducer(fetcher):
    """A terminal reproducer label alongside in-progress means the stage
    finished; do not treat the leftover in-progress marker as abandoned."""
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(
        issue,
        [
            JiraLabels.TRIAGED_REBASE.value,
            JiraLabels.REPRODUCER_IN_PROGRESS.value,
            JiraLabels.REPRODUCER_CREATED.value,
        ],
    )
    assert result is None


def test_find_stale_reproducer_in_progress_alone(fetcher):
    issue = {"fields": {"updated": _STALE_UPDATED}}
    result = fetcher._find_stale_in_flight_label(issue, [JiraLabels.REPRODUCER_IN_PROGRESS.value])
    assert result == JiraLabels.REPRODUCER_IN_PROGRESS.value


def test_find_stale_reproducer_in_progress_fresh_not_recovered(fetcher):
    issue = {"fields": {"updated": _FRESH_UPDATED}}
    result = fetcher._find_stale_in_flight_label(
        issue,
        [JiraLabels.TRIAGED_NOT_AFFECTED.value, JiraLabels.REPRODUCER_IN_PROGRESS.value],
    )
    assert result is None


# ============================================================================
# push_issues_to_queue: stale in-flight label safety net
# ============================================================================


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


def _consolidation_pair(
    base_component="curl",
    next_component="curl",
    base_downstream="",
    next_downstream="",
):
    """Build a base/next issue pair eligible for label-triggered consolidation."""
    base = {
        "key": "BASE-1",
        "fields": {
            "labels": [JiraLabels.CONSOLIDATE_BASE.value],
            "components": [{"name": base_component}],
            "customfield_10669": base_downstream,
            "fixVersions": [{"name": "rhel-9.8"}],
        },
    }
    next_ = {
        "key": "NEXT-1",
        "fields": {
            "labels": [JiraLabels.CONSOLIDATE_NEXT.value],
            "components": [{"name": next_component}],
            "customfield_10669": next_downstream,
            "fixVersions": [{"name": "rhel-9.8"}],
        },
    }
    return [base, next_]


@pytest.mark.asyncio
async def test_process_consolidation_labels_submits_job_for_non_modular_pair(fetcher, mock_redis_context):
    mock_redis, _ = mock_redis_context
    issues = _consolidation_pair()

    flexmock(fetcher).should_receive("_label_added_by_rh_employee").and_return(True)
    flexmock(jira_issue_fetcher_impl).should_receive("submit_merge_job").with_args(
        mock_redis, "curl", "rhel-9.8.0", source_issues=["BASE-1", "NEXT-1"]
    ).and_return(create_async_mock_return_value(jira_issue_fetcher_impl.SubmitResult.SUBMITTED)).once()
    flexmock(fetcher).should_receive("_edit_jira_labels")
    flexmock(fetcher).should_receive("_post_jira_comment")

    submitted = await fetcher._process_consolidation_labels(issues)

    assert submitted == 1


@pytest.mark.asyncio
async def test_process_consolidation_labels_skips_modular_base_issue(fetcher, mock_redis_context):
    """A modular downstream component (module:stream/package) on either issue must
    block the pairing — consolidation's Copr build doesn't enable module streams."""
    _, _ = mock_redis_context
    issues = _consolidation_pair(base_downstream="postgresql:16/postgis")

    flexmock(fetcher).should_receive("_label_added_by_rh_employee").and_return(True)
    flexmock(jira_issue_fetcher_impl).should_receive("submit_merge_job").never()
    flexmock(fetcher).should_receive("_edit_jira_labels").never()
    flexmock(fetcher).should_receive("_post_jira_comment").never()

    submitted = await fetcher._process_consolidation_labels(issues)

    assert submitted == 0


@pytest.mark.asyncio
async def test_process_consolidation_labels_skips_modular_next_issue(fetcher, mock_redis_context):
    _, _ = mock_redis_context
    issues = _consolidation_pair(next_downstream="postgresql:16/postgis")

    flexmock(fetcher).should_receive("_label_added_by_rh_employee").and_return(True)
    flexmock(jira_issue_fetcher_impl).should_receive("submit_merge_job").never()
    flexmock(fetcher).should_receive("_edit_jira_labels").never()
    flexmock(fetcher).should_receive("_post_jira_comment").never()

    submitted = await fetcher._process_consolidation_labels(issues)

    assert submitted == 0


@pytest.mark.asyncio
async def test_fetcher_uses_temporal_for_all_eligible_issues(fetcher, mock_redis_context):
    fetcher.max_issues = 2
    client = object()
    submitted = []
    trigger = JiraLabels.TODO_TRIAGE.value

    async def connect():
        return client

    async def submit(connection, intent):
        assert connection is client
        submitted.append(intent)
        return "accepted"

    flexmock(fetcher).should_receive("authorized_trigger_event").with_args("RHEL-2", trigger).and_return("67")
    flexmock(fetcher).should_receive("_edit_jira_labels").never()
    flexmock(jira_issue_fetcher_impl).should_receive("connect_triage_ingress").replace_with(connect)
    flexmock(jira_issue_fetcher_impl).should_receive("submit_triage_intent").replace_with(submit)

    async def existing(_redis):
        return set()

    flexmock(fetcher).should_receive("_get_existing_issue_keys").replace_with(existing)
    issues = [
        {"key": "RHEL-4", "fields": {"labels": [JiraLabels.BACKPORTED.value]}},
        {"key": "RHEL-1", "fields": {"updated": "2026-10-01"}},
        {"key": "RHEL-1", "fields": {"updated": "2026-10-01"}},
        {"key": "RHEL-2", "fields": {"labels": [trigger, JiraLabels.TRIAGE_IN_PROGRESS.value]}},
        {"key": "RHEL-3", "fields": {}},
    ]
    assert await fetcher.submit_temporal_issues(issues) == 2
    discovery, manual = submitted
    assert discovery.issue == "RHEL-1" and discovery.request_id == "discovery:2026-10-01"
    assert manual.issue == "RHEL-2" and manual.request_id == f"jira:{trigger}:67"
    assert manual.user_triggered and manual.triage_only and manual.triggers == {trigger: "67"}
