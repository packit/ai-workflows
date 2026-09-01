"""Unit tests for Z-stream status check functions (PACKIT-5281)."""

import os

import pytest
from beeai_framework.tools import JSONToolOutput
from flexmock import flexmock

from ymir.tools.privileged import jira as jira_tools
from ymir.tools.privileged.jira import (
    SearchJiraIssuesTool,
    _check_zstream_not_affected,
    _check_zstream_pending_triage,
    _get_applicable_zstream_variants,
)


def _create_async_return(value):
    """Create a coroutine that returns the given value when awaited."""

    async def async_return(*args, **kwargs):
        return value

    return async_return()


RHEL_CONFIG = {
    "current_y_streams": {"9": "rhel-9.8", "10": "rhel-10.2", "11": "rhel-11.2"},
    "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.6.z", "11": "rhel-11.1.z"},
    "upcoming_z_streams": {"9": "rhel-9.7.z", "10": "rhel-10.3.z"},
}


@pytest.fixture(autouse=True)
def mocked_env():
    flexmock(os).should_receive("getenv").with_args("JIRA_URL").and_return("http://jira")
    flexmock(jira_tools).should_receive("get_jira_auth_headers").and_return(
        {
            "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbToxMjM0NQ==",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )


# Tests for _get_applicable_zstream_variants()


@pytest.mark.asyncio
async def test_get_applicable_zstream_variants_upcoming_wins():
    """Upcoming Z-stream takes precedence over current when both exist."""
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    variants = await _get_applicable_zstream_variants("9")
    # get_fix_version_variants returns both Y and Z forms for GA transitions
    assert variants == {"rhel-9.7", "rhel-9.7.z"}


@pytest.mark.asyncio
async def test_get_applicable_zstream_variants_current_fallback():
    """Falls back to current Z-stream when no upcoming exists (RHEL-11)."""
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    variants = await _get_applicable_zstream_variants("11")
    # get_fix_version_variants returns both Y and Z forms
    assert variants == {"rhel-11.1", "rhel-11.1.z"}


@pytest.mark.asyncio
async def test_get_applicable_zstream_variants_no_zstream():
    """Returns None when no Z-stream exists for major version."""
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    variants = await _get_applicable_zstream_variants("7")
    assert variants is None


@pytest.mark.asyncio
async def test_get_applicable_zstream_variants_maintenance():
    """Returns None when major version is in maintenance (has Z-stream but no Y-stream)."""
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    # Version 8 has current_z_streams but no current_y_streams = maintenance
    variants = await _get_applicable_zstream_variants("8")
    assert variants is None


# Tests for _check_zstream_not_affected()


@pytest.mark.asyncio
async def test_check_zstream_not_affected_found():
    """Z-stream clone with ymir_triaged_not_affected label is found."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    not_affected = await _check_zstream_not_affected("CVE-2026-12345", "curl", "RHEL-999", "9")
    assert not_affected == ["RHEL-111"]


@pytest.mark.asyncio
async def test_check_zstream_not_affected_none_found():
    """No Z-stream clones with ymir_triaged_not_affected label exist."""
    search_result = []
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    not_affected = await _check_zstream_not_affected("CVE-2026-12345", "curl", "RHEL-999", "9")
    assert not_affected == []


@pytest.mark.asyncio
async def test_check_zstream_not_affected_clone_is_affected():
    """Z-stream clone exists but doesn't have ymir_triaged_not_affected label (is affected)."""
    # Search returns empty because JQL requires ymir_triaged_not_affected label
    search_result = []
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    not_affected = await _check_zstream_not_affected("CVE-2026-12345", "curl", "RHEL-999", "9")
    assert not_affected == []


@pytest.mark.asyncio
async def test_check_zstream_not_affected_wrong_version():
    """Z-stream clone with wrong fix version is filtered out."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-8.10.z"}],  # Wrong major version
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    not_affected = await _check_zstream_not_affected("CVE-2026-12345", "curl", "RHEL-999", "9")
    assert not_affected == []


@pytest.mark.asyncio
async def test_check_zstream_not_affected_old_current_ignored():
    """Old current Z-stream clone is ignored when upcoming exists."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.6.z"}],  # Old current
            },
        },
        {
            "key": "RHEL-222",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],  # Upcoming (wins)
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    not_affected = await _check_zstream_not_affected("CVE-2026-12345", "curl", "RHEL-999", "9")
    # Only the upcoming Z-stream (9.7.z) should be returned
    assert not_affected == ["RHEL-222"]


@pytest.mark.asyncio
async def test_check_zstream_not_affected_no_applicable_zstream():
    """Returns empty list when no applicable Z-stream exists (returns early, no Jira search)."""
    # Mock SearchJiraIssuesTool to ensure it's NOT called
    search_mock = flexmock(SearchJiraIssuesTool)
    search_mock.should_receive("run").never()

    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    # Version 7 has no Z-stream in config, should return early
    not_affected = await _check_zstream_not_affected("CVE-2026-12345", "curl", "RHEL-999", "7")
    assert not_affected == []


# Tests for _check_zstream_pending_triage()


@pytest.mark.asyncio
async def test_check_zstream_pending_triage_found():
    """Z-stream clone without terminal labels is found as pending."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
                "labels": ["SecurityTracking", "ymir_triage_in_progress"],
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    pending = await _check_zstream_pending_triage("CVE-2026-12345", "curl", "RHEL-999", "9")
    assert pending == ["RHEL-111"]


@pytest.mark.asyncio
async def test_check_zstream_pending_triage_terminal_excluded():
    """Z-stream clones with terminal labels are excluded from pending list."""
    search_result = []  # JQL already excludes terminal labels
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    pending = await _check_zstream_pending_triage("CVE-2026-12345", "curl", "RHEL-999", "9")
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_pending_triage_none_found():
    """No pending Z-stream clones exist."""
    search_result = []
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    pending = await _check_zstream_pending_triage("CVE-2026-12345", "curl", "RHEL-999", "9")
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_pending_triage_clone_is_affected():
    """Z-stream clone exists but has terminal label (triage completed, is affected)."""
    # Search returns empty because JQL excludes terminal labels like ymir_triaged_backport
    search_result = []
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    pending = await _check_zstream_pending_triage("CVE-2026-12345", "curl", "RHEL-999", "9")
    assert pending == []


@pytest.mark.asyncio
async def test_check_zstream_pending_triage_old_current_ignored():
    """Old current Z-stream clone is ignored when upcoming exists."""
    search_result = [
        {
            "key": "RHEL-111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.6.z"}],  # Old current
                "labels": ["SecurityTracking"],
            },
        },
        {
            "key": "RHEL-222",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],  # Upcoming (wins)
                "labels": ["SecurityTracking"],
            },
        },
    ]
    flexmock(SearchJiraIssuesTool).should_receive("run").and_return(
        _create_async_return(JSONToolOutput(result=search_result))
    ).once()
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    pending = await _check_zstream_pending_triage("CVE-2026-12345", "curl", "RHEL-999", "9")
    # Only the upcoming Z-stream (9.7.z) should be returned
    assert pending == ["RHEL-222"]


@pytest.mark.asyncio
async def test_check_zstream_pending_triage_no_applicable_zstream():
    """Returns empty list when no applicable Z-stream exists (returns early, no Jira search)."""
    # Mock SearchJiraIssuesTool to ensure it's NOT called
    search_mock = flexmock(SearchJiraIssuesTool)
    search_mock.should_receive("run").never()

    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    # Version 7 has no Z-stream in config, should return early
    pending = await _check_zstream_pending_triage("CVE-2026-12345", "curl", "RHEL-999", "7")
    assert pending == []


# Tests for Jira search failure cases


@pytest.mark.asyncio
async def test_check_zstream_not_affected_jira_search_failure():
    """Raises exception when Jira search fails."""
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    # Simulate Jira search failure
    async def raise_error(*args, **kwargs):
        raise RuntimeError("Jira API connection timeout")

    flexmock(SearchJiraIssuesTool).should_receive("run").replace_with(raise_error).once()

    with pytest.raises(RuntimeError, match="Jira API connection timeout"):
        await _check_zstream_not_affected("CVE-2026-12345", "curl", "RHEL-999", "9")


@pytest.mark.asyncio
async def test_check_zstream_pending_triage_jira_search_failure():
    """Raises exception when Jira search fails."""
    flexmock(jira_tools).should_receive("load_rhel_config").and_return(
        _create_async_return(RHEL_CONFIG)
    ).once()

    # Simulate Jira search failure
    async def raise_error(*args, **kwargs):
        raise RuntimeError("Jira server unavailable")

    flexmock(SearchJiraIssuesTool).should_receive("run").replace_with(raise_error).once()

    with pytest.raises(RuntimeError, match="Jira server unavailable"):
        await _check_zstream_pending_triage("CVE-2026-12345", "curl", "RHEL-999", "9")
