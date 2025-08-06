import os

import pytest
import requests
from flexmock import flexmock

from jira_tools import Severity, PreliminaryTesting, get_jira_details, set_jira_fields, add_jira_comment


@pytest.fixture(autouse=True)
def mocked_env():
    flexmock(os).should_receive("getenv").with_args("JIRA_URL").and_return("http://jira")
    flexmock(os).should_receive("getenv").with_args("JIRA_TOKEN").and_return("12345")


def test_get_jira_details():
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

    def get(url, params=None, headers=None):
        if url.endswith(f"rest/api/2/issue/{issue_key}"):
            assert params.get("expand") == "comments"
            return flexmock(json=lambda: issue_data, raise_for_status=lambda: None)
        elif url.endswith(f"rest/api/2/issue/{issue_key}/remotelink"):
            return flexmock(json=lambda: remote_links_data, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    flexmock(requests).should_receive("get").replace_with(get)

    result = get_jira_details(issue_key)
    expected_result = issue_data.copy()
    expected_result["remote_links"] = remote_links_data

    assert result == expected_result


@pytest.mark.parametrize(
    "args, fields",
    [
        (
            dict(fix_versions=["rhel-1.2.3"]),
            {"fixVersions": [{"name": "rhel-1.2.3"}]},
        ),
        (
            dict(severity=Severity.LOW),
            {"customfield_12316142": {"value": Severity.LOW.value}},
        ),
        (
            dict(preliminary_testing=PreliminaryTesting.FAIL),
            {"customfield_12321540": {"value": PreliminaryTesting.FAIL.value}},
        ),
    ],
)
def test_set_jira_fields(args, fields):
    issue_key = "RHEL-12345"

    def put(url, json, headers):
        assert url.endswith(f"rest/api/2/issue/{issue_key}")
        assert json.get("fields") == fields
        return flexmock(raise_for_status=lambda: None)

    flexmock(requests).should_receive("put").replace_with(put)
    result = set_jira_fields(issue_key, **args)
    assert result.startswith("Successfully")


def test_add_jira_comment():
    issue_key = "RHEL-12345"
    comment = "Test comment"

    def post(url, json, headers):
        assert url.endswith(f"rest/api/2/issue/{issue_key}/comment")
        assert json.get("body") == comment
        return flexmock(raise_for_status=lambda: None)

    flexmock(requests).should_receive("post").replace_with(post)
    result = add_jira_comment(issue_key, comment)
    assert result.startswith("Successfully")
