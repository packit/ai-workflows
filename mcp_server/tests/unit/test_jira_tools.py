import os
import datetime
from contextlib import asynccontextmanager

import aiohttp
import pytest
from flexmock import flexmock

from jira_tools import Severity, PreliminaryTesting, get_jira_details, set_jira_fields, add_jira_comment, change_jira_status, edit_jira_labels, verify_issue_author


@pytest.fixture(autouse=True)
def mocked_env():
    flexmock(os).should_receive("getenv").with_args("JIRA_URL").and_return("http://jira")
    flexmock(os).should_receive("getenv").with_args("JIRA_TOKEN").and_return("12345")
    flexmock(os).should_receive("getenv").with_args("DRY_RUN", "False").and_return("false")


@pytest.mark.asyncio
async def test_get_jira_details():
    issue_key = "RHEL-12345"
    issue_data = {
        "key": issue_key,
        "id": "12345",
        "fields": {"summary": "Test issue"},
        "comment": {"comments": [{"body": "Test comment"}], "total": 1},
    }
    remote_links_data = [
        {
            "id": 10000,
            "object": {
                "url": "https://github.com/example/repo/pull/123",
                "title": "Fix issue RHEL-12345"
            }
        }
    ]

    @asynccontextmanager
    async def get(url, params=None, headers=None):
        if url.endswith(f"rest/api/2/issue/{issue_key}"):
            assert params.get("expand") == "comments"
            async def json():
                return issue_data
            yield flexmock(json=json, raise_for_status=lambda: None)
        elif url.endswith(f"rest/api/2/issue/{issue_key}/remotelink"):
            async def json():
                return remote_links_data
            yield flexmock(json=json, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await get_jira_details(issue_key)
    expected_result = issue_data.copy()
    expected_result["remote_links"] = remote_links_data

    assert result == expected_result


@pytest.mark.parametrize(
    "args, current_fields, expected_fields",
    [
        (
            dict(fix_versions=["rhel-1.2.3"]),
            {"fields": {"fixVersions": []}},
            {"fixVersions": [{"name": "rhel-1.2.3"}]},
        ),
        (
            dict(severity=Severity.LOW),
            {"fields": {"customfield_12316142": {"value": None}}},
            {"customfield_12316142": {"value": Severity.LOW.value}},
        ),
        (
            dict(target_end=datetime.date(2024, 12, 31)),
            {"fields": {"customfield_12313942": {"value": None}}},
            {"customfield_12313942": "2024-12-31"},
        ),
        (
            dict(fix_versions=["rhel-1.2.3"], severity=Severity.CRITICAL),
            {"fields": {"fixVersions": [], "customfield_12316142": {"value": None}}},
            {"fixVersions": [{"name": "rhel-1.2.3"}], "customfield_12316142": {"value": Severity.CRITICAL.value}},
        ),
    ],
)
@pytest.mark.asyncio
async def test_set_jira_fields(args, current_fields, expected_fields):
    issue_key = "RHEL-12345"

    @asynccontextmanager
    async def get(url, headers=None):
        if url.endswith(f"rest/api/2/issue/{issue_key}"):
            async def json():
                return current_fields
            yield flexmock(json=json, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    @asynccontextmanager
    async def put(url, json, headers):
        assert url.endswith(f"rest/api/2/issue/{issue_key}")
        assert json.get("fields") == expected_fields
        yield flexmock(raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)
    flexmock(aiohttp.ClientSession).should_receive("put").replace_with(put)
    result = await set_jira_fields(issue_key, **args)
    assert result.startswith("Successfully")


@pytest.mark.parametrize(
    "private", [False, True],
)
@pytest.mark.asyncio
async def test_add_jira_comment(private):
    issue_key = "RHEL-12345"
    comment = "Test comment"

    @asynccontextmanager
    async def post(url, json, headers):
        assert url.endswith(f"rest/api/2/issue/{issue_key}/comment")
        assert json.get("body") == comment
        if private:
            assert json.get("visibility") == {"type": "group", "value": "Red Hat Employee"}
        yield flexmock(raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(post)
    result = await add_jira_comment(issue_key, comment, private)
    assert result.startswith("Successfully")


@pytest.mark.parametrize(
    "transitions, status, expected_transition_id",
    [
        (
            [
                {"id": "11", "to": {"name": "In Progress"}},
                {"id": "21", "to": {"name": "Done"}},
                {"id": "31", "to": {"name": "Closed"}},
            ],
            "In Progress",
            "11",
        ),
        (
            [
                {"id": "11", "to": {"name": "In Progress"}},
                {"id": "21", "to": {"name": "Done"}},
            ],
            "done",
            "21",
        ),
    ],
)
@pytest.mark.asyncio
async def test_change_jira_status(transitions, status, expected_transition_id):
    issue_key = "RHEL-12345"

    current_status_data = {
        "fields": {
            "status": {"name": "To Do"}
        }
    }

    @asynccontextmanager
    async def get(url, params=None, headers=None):
        if url.endswith(f"rest/api/2/issue/{issue_key}") and params and params.get("fields") == "status":
            async def json():
                return current_status_data
            yield flexmock(json=json, raise_for_status=lambda: None)
        elif url.endswith(f"rest/api/2/issue/{issue_key}/transitions"):
            async def json():
                return {"transitions": transitions}
            yield flexmock(json=json, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    @asynccontextmanager
    async def post(url, json, headers):
        assert url.endswith(f"rest/api/2/issue/{issue_key}/transitions")
        assert json.get("transition", {}).get("id") == expected_transition_id
        yield flexmock(raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)
    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(post)

    result = await change_jira_status(issue_key, status)
    assert result.startswith("Successfully")



@pytest.mark.parametrize(
    "labels_to_add, labels_to_remove, expected_update_payload",
    [
        (
            ["new-label"],
            None,
            [{"add": "new-label"}],
        ),
        (
            None,
            ["to-remove"],
            [{"remove": "to-remove"}],
        ),
        (
            ["new-label1", "new-label2"],
            ["to-remove1", "to-remove2"],
            [{"add": "new-label1"}, {"add": "new-label2"}, {"remove": "to-remove1"}, {"remove": "to-remove2"}],
        ),
    ],
)
@pytest.mark.asyncio
async def test_edit_jira_labels(labels_to_add, labels_to_remove, expected_update_payload):
    issue_key = "RHEL-12345"

    @asynccontextmanager
    async def put(url, json, headers):
        assert url.endswith(f"rest/api/2/issue/{issue_key}")
        assert json.get("update", {}).get("labels") == expected_update_payload
        yield flexmock(raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("put").replace_with(put)

    result = await edit_jira_labels(issue_key, labels_to_add, labels_to_remove)
    assert result.startswith("Successfully")


@pytest.mark.parametrize(
    "user_groups, expected_result, use_account_id",
    [
        # Jira Server (key-based)
        (["Red Hat Employee", "Other Group"], True, False),
        (["Other Group", "Red Hat Employee"], True, False),
        (["Some Group", "Other Group"], False, False),
        ([], False, False),
        # Jira Cloud (accountId-based)
        (["Red Hat Employee", "Other Group"], True, True),
        (["Some Group", "Other Group"], False, True),
    ],
)
@pytest.mark.asyncio
async def test_verify_issue_author(user_groups, expected_result, use_account_id):
    issue_key = "RHEL-12345"

    reporter = {}
    expected_param_key = None
    expected_param_value = None
    
    if use_account_id:
        reporter["accountId"] = "test-account-id-123"
        expected_param_key = "accountId"
        expected_param_value = "test-account-id-123"
    else:
        reporter["key"] = "test-user-key"
        expected_param_key = "key"
        expected_param_value = "test-user-key"

    issue_data = {
        "fields": {
            "reporter": reporter
        }
    }

    user_data = {
        "groups": {
            "size": len(user_groups),
            "items": [{"name": group} for group in user_groups]
        }
    }

    @asynccontextmanager
    async def get(url, params=None, headers=None):
        if url.endswith(f"rest/api/2/issue/{issue_key}"):
            async def json():
                return issue_data
            yield flexmock(json=json, raise_for_status=lambda: None)
        elif url.endswith("rest/api/2/user"):
            assert params.get(expected_param_key) == expected_param_value
            assert params.get("expand") == "groups"
            async def json():
                return user_data
            yield flexmock(json=json, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await verify_issue_author(issue_key)
    assert result == expected_result
