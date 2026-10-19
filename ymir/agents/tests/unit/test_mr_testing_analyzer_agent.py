"""Unit tests for the mr_testing_analyzer agent's workflow steps."""

import contextlib
import json

import pytest
from flexmock import flexmock

from ymir.agents import mr_testing_analyzer_agent as m
from ymir.agents.mr_testing_analyzer_agent import (
    MrTestingAnalyzerResult,
    _extract_linked_issues,
    run_mr_testing_analyzer,
)


@pytest.mark.parametrize(
    "description,expected",
    [
        ("No Jira line here", []),
        ("Fixes a bug.\n\nResolves: RHEL-12345", ["RHEL-12345"]),
        ("Resolves: RHEL-1\nResolves: RHEL-2", ["RHEL-1", "RHEL-2"]),
        ("resolves: rhel-999", ["RHEL-999"]),
        ("Resolves: RHEL-1\nResolves: RHEL-1", ["RHEL-1"]),
        ("", []),
    ],
)
def test_extract_linked_issues(description, expected):
    assert _extract_linked_issues(description) == expected


@contextlib.asynccontextmanager
async def _mock_mcp_tools(*_args, **_kwargs):
    yield [flexmock(name="dummy-gateway-tool")]


@pytest.mark.asyncio
async def test_no_linked_issues_bails_out_cleanly(monkeypatch):
    """No Resolves: line in the MR description -> clean bail-out, no further tool calls."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append(name)
        if name == "get_merge_request_details":
            return {"description": "Just a plain MR with no linked issue."}
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyzer("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is False
    assert calls == ["get_merge_request_details"]


@pytest.mark.asyncio
async def test_linked_issues_gather_jira_context_and_thread_flags(monkeypatch):
    """Linked issues are resolved to CVE id / Test Coverage, and dry_run/poc/
    no_jira_comments flags make it into the prompt handed to the ReasoningAgent."""
    jira_fields = {
        "RHEL-111": {
            "summary": "CVE-2024-12345 some overflow",
            "labels": [],
            "customfield_10638": [{"value": "Automated"}],
        },
        "RHEL-222": {
            "summary": "no cve here",
            "labels": [],
            "customfield_10638": None,
        },
    }

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        if name == "get_merge_request_details":
            return {"description": "Resolves: RHEL-111\nResolves: RHEL-222"}
        if name == "get_authorized_comments_from_merge_request":
            return {"comments": [{"body": "**ANEWA:** testing completed.", "created_at": "2024-01-01T00:00:00Z"}]}
        if name == "get_jira_details":
            return {"fields": jira_fields[kwargs["issue_key"]]}
        raise AssertionError(f"Unexpected tool call: {name}")

    captured_prompt = {}

    class _FakeMessage:
        def __init__(self, text):
            self.text = text

    class _FakeResponse:
        def __init__(self, text):
            self.last_message = _FakeMessage(text)

    class _FakeAgent:
        async def run(self, prompt, **kwargs):
            captured_prompt["prompt"] = prompt
            return _FakeResponse(
                MrTestingAnalyzerResult(analyzed=True, summary="posted everything").model_dump_json()
            )

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(m).should_receive("create_mr_testing_analyzer_agent").and_return(_FakeAgent())
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyzer(
        "https://gitlab.com/x/y/-/merge_requests/1",
        dry_run=True,
        poc=True,
        no_jira_comments=True,
    )

    assert result.analyzed is True
    assert result.summary == "posted everything"

    prompt = captured_prompt["prompt"]
    assert "RHEL-111" in prompt
    assert "CVE-2024-12345" in prompt
    assert "Automated" in prompt
    assert "RHEL-222" in prompt
    assert "dry_run: True" in prompt
    assert "poc: True" in prompt
    assert "no_jira_comments: True" in prompt
    # Raw MR comments are appended for the agent's own parse_mr_test_results call.
    assert "**ANEWA:** testing completed." in prompt
    comments_json = prompt.split("MR_COMMENTS (JSON, pass as-is to parse_mr_test_results): ", 1)[1]
    assert json.loads(comments_json) == [
        {"body": "**ANEWA:** testing completed.", "created_at": "2024-01-01T00:00:00Z"}
    ]


@pytest.mark.asyncio
async def test_fetch_test_results_falls_back_to_mr_notes(monkeypatch):
    """If get_authorized_comments_from_merge_request fails, fall back to fetch_gitlab_mr_notes."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append(name)
        if name == "get_merge_request_details":
            return {"description": "Resolves: RHEL-333"}
        if name == "get_authorized_comments_from_merge_request":
            raise RuntimeError("boom")
        if name == "fetch_gitlab_mr_notes":
            return {"comments": []}
        if name == "get_jira_details":
            return {"fields": {"summary": "", "labels": [], "customfield_10638": None}}
        raise AssertionError(f"Unexpected tool call: {name}")

    class _FakeMessage:
        def __init__(self, text):
            self.text = text

    class _FakeResponse:
        def __init__(self, text):
            self.last_message = _FakeMessage(text)

    class _FakeAgent:
        async def run(self, prompt, **kwargs):
            return _FakeResponse(
                MrTestingAnalyzerResult(analyzed=True, summary="done").model_dump_json()
            )

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(m).should_receive("create_mr_testing_analyzer_agent").and_return(_FakeAgent())
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyzer("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is True
    assert "get_authorized_comments_from_merge_request" in calls
    assert "fetch_gitlab_mr_notes" in calls
