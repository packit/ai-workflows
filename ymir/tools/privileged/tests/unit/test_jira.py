import datetime
import os
from contextlib import asynccontextmanager

import aiohttp
import pytest
from beeai_framework.tools import JSONToolOutput
from flexmock import flexmock

from ymir.common.models import TriageEligibility
from ymir.tools.privileged import jira as jira_tools
from ymir.tools.privileged.jira import (
    AddJiraCommentTool,
    ChangeJiraStatusTool,
    CheckCveTriageEligibilityTool,
    EditJiraLabelsTool,
    GetJiraDetailsTool,
    SearchJiraIssuesTool,
    SetJiraFieldsTool,
    Severity,
    VerifyIssueAuthorTool,
    _check_zstream_clones_shipped,
    _extract_cve_id,
)


def _create_async_return(value):
    """Create a coroutine that returns the given value when awaited."""

    async def async_return(*args, **kwargs):
        return value

    return async_return()


@pytest.fixture(autouse=True)
def mocked_env():
    flexmock(os).should_receive("getenv").with_args("JIRA_URL").and_return("http://jira")
    flexmock(os).should_receive("getenv").with_args("DRY_RUN", "False").and_return("false")
    flexmock(os).should_receive("getenv").with_args("SKIP_SETTING_JIRA_FIELDS", "False").and_return("false")
    flexmock(os).should_receive("getenv").with_args("JIRA_DRY_RUN", "False").and_return("false")
    flexmock(jira_tools).should_receive("get_jira_auth_headers").and_return(
        {
            "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbToxMjM0NQ==",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )


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
                "title": "Fix issue RHEL-12345",
            },
        }
    ]

    @asynccontextmanager
    async def get(url, params=None, headers=None):
        if url.endswith(f"rest/api/3/issue/{issue_key}"):
            assert params.get("expand") == "comments"

            async def json():
                return issue_data

            yield flexmock(json=json, raise_for_status=lambda: None)
        elif url.endswith(f"rest/api/3/issue/{issue_key}/remotelink"):

            async def json():
                return remote_links_data

            yield flexmock(json=json, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = (await GetJiraDetailsTool().run(input={"issue_key": issue_key})).result
    expected_result = issue_data.copy()
    expected_result["remote_links"] = remote_links_data

    assert result == expected_result


@pytest.mark.parametrize(
    "args, current_fields, expected_fields",
    [
        (
            {"fix_versions": ["rhel-1.2.3"]},
            {"fields": {"fixVersions": []}},
            {"fixVersions": [{"name": "rhel-1.2.3"}]},
        ),
        (
            {"severity": Severity.LOW},
            {"fields": {"customfield_10840": {"value": None}}},
            {"customfield_10840": {"value": Severity.LOW.value}},
        ),
        (
            {"target_end": datetime.date(2024, 12, 31)},
            {"fields": {"customfield_10023": {"value": None}}},
            {"customfield_10023": "2024-12-31"},
        ),
        (
            {"fix_versions": ["rhel-1.2.3"], "severity": Severity.CRITICAL},
            {"fields": {"fixVersions": [], "customfield_10840": {"value": None}}},
            {
                "fixVersions": [{"name": "rhel-1.2.3"}],
                "customfield_10840": {"value": Severity.CRITICAL.value},
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_set_jira_fields(args, current_fields, expected_fields):
    issue_key = "RHEL-12345"

    @asynccontextmanager
    async def get(url, headers=None):
        if url.endswith(f"rest/api/3/issue/{issue_key}"):

            async def json():
                return current_fields

            yield flexmock(json=json, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    @asynccontextmanager
    async def put(url, json, headers):
        assert url.endswith(f"rest/api/3/issue/{issue_key}")
        assert json.get("fields") == expected_fields
        yield flexmock(ok=True, raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)
    flexmock(aiohttp.ClientSession).should_receive("put").replace_with(put)
    result = (await SetJiraFieldsTool().run(input={"issue_key": issue_key, **args})).result
    assert result.startswith("Successfully")


@pytest.mark.parametrize(
    "private",
    [False, True],
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
            assert json.get("visibility") == {
                "type": "group",
                "value": "Red Hat Employee",
            }
        yield flexmock(raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(post)
    result = (
        await AddJiraCommentTool().run(input={"issue_key": issue_key, "comment": comment, "private": private})
    ).result
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

    current_status_data = {"fields": {"status": {"name": "To Do"}}}

    @asynccontextmanager
    async def get(url, params=None, headers=None):
        if url.endswith(f"rest/api/3/issue/{issue_key}") and params and params.get("fields") == "status":

            async def json():
                return current_status_data

            yield flexmock(json=json, raise_for_status=lambda: None)
        elif url.endswith(f"rest/api/3/issue/{issue_key}/transitions"):

            async def json():
                return {"transitions": transitions}

            yield flexmock(json=json, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    @asynccontextmanager
    async def post(url, json, headers):
        assert url.endswith(f"rest/api/3/issue/{issue_key}/transitions")
        assert json.get("transition", {}).get("id") == expected_transition_id
        yield flexmock(raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)
    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(post)

    result = (await ChangeJiraStatusTool().run(input={"issue_key": issue_key, "status": status})).result
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
            [
                {"add": "new-label1"},
                {"add": "new-label2"},
                {"remove": "to-remove1"},
                {"remove": "to-remove2"},
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_edit_jira_labels(labels_to_add, labels_to_remove, expected_update_payload):
    issue_key = "RHEL-12345"

    @asynccontextmanager
    async def put(url, json, headers):
        assert url.endswith(f"rest/api/3/issue/{issue_key}")
        assert json.get("update", {}).get("labels") == expected_update_payload
        yield flexmock(raise_for_status=lambda: None)

    flexmock(aiohttp.ClientSession).should_receive("put").replace_with(put)

    result = (
        await EditJiraLabelsTool().run(
            input={
                "issue_key": issue_key,
                "labels_to_add": labels_to_add,
                "labels_to_remove": labels_to_remove,
            }
        )
    ).result
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

    issue_data = {"fields": {"reporter": reporter}}

    user_data = {
        "groups": {
            "size": len(user_groups),
            "items": [{"name": group} for group in user_groups],
        }
    }

    @asynccontextmanager
    async def get(url, params=None, headers=None):
        if url.endswith(f"rest/api/3/issue/{issue_key}"):

            async def json():
                return issue_data

            yield flexmock(json=json, raise_for_status=lambda: None)
        elif url.endswith("rest/api/3/user"):
            assert params.get(expected_param_key) == expected_param_value
            assert params.get("expand") == "groups"

            async def json():
                return user_data

            yield flexmock(json=json, raise_for_status=lambda: None)
        else:
            raise AssertionError(f"Unexpected URL: {url}")

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = (await VerifyIssueAuthorTool().run(input={"issue_key": issue_key})).result
    assert result == expected_result


# --- CVE ID extraction tests ---


@pytest.mark.parametrize(
    "summary, expected",
    [
        ("CVE-2025-12345 buffer overflow in curl [rhel-9.8]", "CVE-2025-12345"),
        ("kernel: fix for CVE-2025-99999 buffer overflow", "CVE-2025-99999"),
        ("No CVE here", None),
        ("Multiple CVE-2025-1111 and CVE-2025-2222", "CVE-2025-1111"),
        ("CVE-2024-1 too short", None),
    ],
)
def test_extract_cve_id(summary, expected):
    assert _extract_cve_id(summary) == expected


# --- Z-stream clone check tests ---

RHEL_CONFIG = {
    "current_y_streams": {"9": "rhel-9.8", "10": "rhel-10.2"},
    "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.6.z"},
    "upcoming_z_streams": {"9": "rhel-9.7.z"},
}


@pytest.mark.asyncio
async def test_check_zstream_clones_all_closed():
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
                "status": {"name": "Closed"},
                "resolution": {"name": "Done-Errata"},
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    any_shipped, pending = await _check_zstream_clones_shipped("CVE-2025-12345", "curl", "RHEL-999")
    assert any_shipped is True
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_clones_one_shipped_one_open():
    """At least one Z-stream clone shipped — proceed even if others are still open."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
                "status": {"name": "Closed"},
                "resolution": {"name": "Done-Errata"},
            },
        },
        {
            "key": "RHEL-222",
            "fields": {
                "fixVersions": [{"name": "rhel-9.6.z"}],
                "status": {"name": "In Progress"},
                "resolution": None,
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    any_shipped, pending = await _check_zstream_clones_shipped("CVE-2025-12345", "curl", "RHEL-999")
    assert any_shipped is True
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_clones_none_shipped():
    """No Z-stream clones shipped — must wait."""
    search_result = [
        {
            "key": "RHEL-222",
            "fields": {
                "fixVersions": [{"name": "rhel-9.6.z"}],
                "status": {"name": "In Progress"},
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    any_shipped, pending = await _check_zstream_clones_shipped("CVE-2025-12345", "curl", "RHEL-999")
    assert any_shipped is False
    assert pending == ["RHEL-222"]


@pytest.mark.asyncio
async def test_check_zstream_clones_none_found():
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=[]))
    ).once()

    any_shipped, pending = await _check_zstream_clones_shipped("CVE-2025-12345", "curl", "RHEL-999")
    assert any_shipped is True
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_clones_eus_filtered_out():
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.2.z"}],
                "status": {"name": "In Progress"},
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    any_shipped, pending = await _check_zstream_clones_shipped("CVE-2025-12345", "curl", "RHEL-999")
    assert any_shipped is True
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_clones_maintenance_filtered_out():
    """RHEL 8 is maintenance (no Y-stream), so rhel-8.10.z clones are excluded."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-8.10.z"}],
                "status": {"name": "In Progress"},
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    any_shipped, pending = await _check_zstream_clones_shipped("CVE-2025-12345", "curl", "RHEL-999")
    assert any_shipped is True
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_clones_closed_wontdo_ignored():
    """Clone closed as Won't Do is neither shipped nor pending — treated as irrelevant."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
                "status": {"name": "Closed"},
                "resolution": {"name": "Won't Do"},
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    any_shipped, pending = await _check_zstream_clones_shipped("CVE-2025-12345", "curl", "RHEL-999")
    assert any_shipped is True
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_clones_wontdo_with_pending():
    """Won't Do clone doesn't count as shipped; pending clone still blocks."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
                "status": {"name": "Closed"},
                "resolution": {"name": "Won't Do"},
            },
        },
        {
            "key": "RHEL-222",
            "fields": {
                "fixVersions": [{"name": "rhel-9.6.z"}],
                "status": {"name": "In Progress"},
                "resolution": None,
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    any_shipped, pending = await _check_zstream_clones_shipped("CVE-2025-12345", "curl", "RHEL-999")
    assert any_shipped is False
    assert pending == ["RHEL-222"]


# --- CVE triage eligibility tests ---


def _make_jira_issue(
    labels=None,
    fix_versions=None,
    summary="Test issue",
    embargo="",
    severity="",
    components=None,
):
    fields = {
        "summary": summary,
        "labels": labels or [],
        "fixVersions": fix_versions or [],
        "components": components or [],
    }
    if embargo:
        fields["customfield_10860"] = {"value": embargo}
    if severity:
        fields["customfield_10840"] = {"value": severity}
    return {"key": "RHEL-12345", "fields": fields}


def _mock_jira_get(issue_data):
    @asynccontextmanager
    async def get(url, headers=None):
        async def json():
            return issue_data

        yield flexmock(json=json, raise_for_status=lambda: None)

    return get


@pytest.mark.asyncio
async def test_eligibility_non_cve():
    issue = _make_jira_issue(labels=["SomeOtherLabel"])
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.IMMEDIATELY
    assert result["is_cve"] is False


@pytest.mark.asyncio
async def test_eligibility_no_fix_version():
    issue = _make_jira_issue(labels=["SecurityTracking"], fix_versions=[])
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.NEVER
    assert result["error"] is not None


@pytest.mark.asyncio
async def test_eligibility_ystream_any_clone_shipped():
    issue = _make_jira_issue(
        labels=["SecurityTracking"],
        fix_versions=[{"name": "rhel-9.8"}],
        summary="CVE-2025-12345 buffer overflow in curl [rhel-9.8]",
        severity="Important",
        components=[{"name": "curl"}],
    )
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))
    flexmock(jira_tools).should_receive("_check_zstream_clones_shipped").with_args(
        "CVE-2025-12345", "curl", "RHEL-12345"
    ).and_return(_create_async_return((True, []))).once()

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.IMMEDIATELY
    assert result["needs_internal_fix"] is True


@pytest.mark.asyncio
async def test_eligibility_ystream_clones_pending():
    issue = _make_jira_issue(
        labels=["SecurityTracking"],
        fix_versions=[{"name": "rhel-9.8"}],
        summary="CVE-2025-12345 buffer overflow in curl [rhel-9.8]",
        severity="Critical",
        components=[{"name": "curl"}],
    )
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))
    flexmock(jira_tools).should_receive("_check_zstream_clones_shipped").with_args(
        "CVE-2025-12345", "curl", "RHEL-12345"
    ).and_return(_create_async_return((False, ["RHEL-999"]))).once()

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.PENDING_DEPENDENCIES
    assert result["pending_zstream_issues"] == ["RHEL-999"]


@pytest.mark.parametrize("severity", ["Low", "Moderate"])
@pytest.mark.asyncio
async def test_eligibility_ystream_low_moderate_skipped(severity):
    issue = _make_jira_issue(
        labels=["SecurityTracking"],
        fix_versions=[{"name": "rhel-9.8"}],
        summary="CVE-2025-12345 buffer overflow in curl [rhel-9.8]",
        severity=severity,
        components=[{"name": "curl"}],
    )
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.NEVER
    assert "CentOS Stream path" in result["reason"]


@pytest.mark.asyncio
async def test_eligibility_ystream_no_cve_id():
    issue = _make_jira_issue(
        labels=["SecurityTracking"],
        fix_versions=[{"name": "rhel-9.8"}],
        summary="Some issue without CVE ID [rhel-9.8]",
        severity="Important",
        components=[{"name": "curl"}],
    )
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.NEVER


@pytest.mark.asyncio
async def test_eligibility_ystream_no_component():
    issue = _make_jira_issue(
        labels=["SecurityTracking"],
        fix_versions=[{"name": "rhel-9.8"}],
        summary="CVE-2025-12345 buffer overflow [rhel-9.8]",
        severity="Critical",
    )
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.NEVER


@pytest.mark.asyncio
async def test_eligibility_embargoed():
    issue = _make_jira_issue(
        labels=["SecurityTracking"],
        fix_versions=[{"name": "rhel-9.7.z"}],
        embargo="True",
    )
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.NEVER


@pytest.mark.asyncio
async def test_eligibility_zstream():
    issue = _make_jira_issue(
        labels=["SecurityTracking"],
        fix_versions=[{"name": "rhel-9.7.z"}],
        severity="moderate",
    )
    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(_mock_jira_get(issue))
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(
            {
                "current_z_streams": {"9": "rhel-9.7.z"},
                "upcoming_z_streams": {},
            }
        )
    ).once()

    result = (await CheckCveTriageEligibilityTool().run(input={"issue_key": "RHEL-12345"})).result
    assert result["eligibility"] == TriageEligibility.IMMEDIATELY
