"""Unit tests for the mr_testing_analyst agent's workflow steps."""

import contextlib
import json
import re

import pytest
from flexmock import flexmock

from ymir.agents import mr_testing_analyst_agent as m
from ymir.agents.mr_testing_analyst_agent import (
    MrCommentToPost,
    MrTestingAnalystResult,
    _extract_linked_issues,
    run_mr_testing_analyst,
)
from ymir.common.models import ErrorListEntry, Task
from ymir.tools.privileged.mr_testing import fold_comment_body

_EMPTY_PARSE_MR_TEST_RESULTS = {
    "nvr": None,
    "results_by_arch": {},
    "unfixed_results_by_arch": {},
    "note_id": None,
    "note_body": None,
    "report_portal_url": None,
}
_NOT_FOUND_PARSE_NEWA = {
    "found": False,
    "comment_id": None,
    "completed": False,
    "all_passed": None,
    "report_portal_url": None,
    "jenkins_url": None,
}
_DEFAULT_PARSE_OSCI = {
    "found": True,
    "comment_id": None,
    "pipeline_id": "123",
    "pipeline_url": "https://gitlab.com/x/y/-/pipelines/123",
    "all_passed": True,
    "banner_all_passed": True,
    "jobs": {},
    "test_jobs": {},
}


@pytest.mark.parametrize(
    "description,expected",
    [
        ("No Jira line here", []),
        ("Fixes a bug.\n\nResolves: RHEL-12345", ["RHEL-12345"]),
        ("Resolves: RHEL-1\nResolves: RHEL-2", ["RHEL-1", "RHEL-2"]),
        ("resolves: rhel-999", ["RHEL-999"]),
        ("Resolves: RHEL-1\nResolves: RHEL-1", ["RHEL-1"]),
        ("", []),
        ("Resolves: RHEL-1, RHEL-2", ["RHEL-1", "RHEL-2"]),
        ("See RHEL-5 for details", []),
    ],
)
def test_extract_linked_issues(description, expected):
    assert _extract_linked_issues(description) == expected


def test_extract_linked_issues_from_commit_messages_and_description():
    commits = [
        "Rebase to 2.0\n\nResolves: RHEL-10\n",
        "Add patch\n\nResolves: RHEL-11\nRelated: RHEL-99\n",
        "No footer here",
    ]
    assert _extract_linked_issues("Resolves: RHEL-12", commits) == ["RHEL-10", "RHEL-11", "RHEL-12"]
    assert _extract_linked_issues("", commits) == ["RHEL-10", "RHEL-11"]


def _render(gating: bool) -> str:
    return m.render_prompt(
        m.InputSchema(
            mr_url="https://gitlab.com/x/y/-/merge_requests/1",
            mr_description="desc",
            linked_issues=[],
            gating=gating,
            parsed_results_json=json.dumps({"found": True}),
        )
    )


def test_render_prompt_gating_only_shows_gating_instructions():
    prompt = _render(gating=True)
    assert "parse_osci_results_comment" in prompt
    assert "Step 8" in prompt
    assert "resolve_osci_discussion" in prompt
    assert "NEWA" not in prompt
    assert "{%" not in prompt


def test_render_prompt_non_gating_hides_gating_instructions():
    prompt = _render(gating=False)
    assert "parse_newa_mr_comment" in prompt
    assert "NEWA" in prompt
    assert "Step 8" not in prompt
    assert "resolve_osci_discussion" not in prompt
    assert "OSCI bot's own" not in prompt
    assert '{"found": true}' in prompt


@contextlib.asynccontextmanager
async def _mock_mcp_tools(*_args, **_kwargs):
    yield [flexmock(name="dummy-gateway-tool")]


def _fake_agent(result: MrTestingAnalystResult):
    class _FakeMessage:
        def __init__(self, text):
            self.text = text

    class _FakeResponse:
        def __init__(self, text):
            self.last_message = _FakeMessage(text)

    class _FakeAgent:
        async def run(self, prompt, **kwargs):
            return _FakeResponse(result.model_dump_json())

    return _FakeAgent()


@pytest.mark.asyncio
async def test_no_linked_issues_bails_out_cleanly_and_labels_mr(monkeypatch):
    """No Resolves: line in the MR description -> clean bail-out. The MR is
    still labeled mr_testing_analyzed (and mr_testing_completed removed) so a
    scheduler polling on mr_testing_completed doesn't re-queue it forever --
    this is the one case fetch_and_validate_mr handles labeling itself,
    before the ReasoningAgent is ever invoked."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append((name, kwargs))
        if name == "get_merge_request_commit_messages":
            return []
        if name == "get_merge_request_details":
            return {"description": "Just a plain MR with no linked issue."}
        if name in ("add_merge_request_labels", "remove_merge_request_labels"):
            return "ok"
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is False
    call_names = [name for name, _ in calls]
    assert call_names == [
        "get_merge_request_details",
        "get_merge_request_commit_messages",
        "add_merge_request_labels",
        "remove_merge_request_labels",
    ]
    add_kwargs = dict(calls[2][1])
    assert add_kwargs["labels"] == ["mr_testing_analyzed"]
    remove_kwargs = dict(calls[3][1])
    assert remove_kwargs["labels"] == ["mr_testing_completed"]


@pytest.mark.asyncio
async def test_commit_fetch_failure_aborts_without_labeling(monkeypatch):
    """A failed commit fetch must not be mistaken for "no linked issues" (which
    would label the MR as analyzed) -- abort so the run can be retried."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append(name)
        if name == "get_merge_request_details":
            return {"description": ""}
        if name == "get_merge_request_commit_messages":
            raise RuntimeError("gitlab down")
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is False
    assert "gitlab down" in result.summary
    assert calls == ["get_merge_request_details", "get_merge_request_commit_messages"]


@pytest.mark.asyncio
async def test_linked_issues_found_only_in_commit_messages(monkeypatch):
    """Keys that appear only in commit messages get past the "no linked
    issues" bail-out (no labeling) and are looked up in Jira."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append((name, kwargs))
        if name == "get_merge_request_details":
            return {"description": "No footer in the description"}
        if name == "get_merge_request_commit_messages":
            return ["Rebase\n\nResolves: RHEL-777\n"]
        if name == "get_authorized_comments_from_merge_request":
            return {"comments": [{"body": "**ANEWA:** done"}]}
        if name == "parse_mr_test_results":
            return {**_EMPTY_PARSE_MR_TEST_RESULTS, "note_id": 1}
        if name == "get_jira_details":
            raise RuntimeError("jira down")
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is False
    assert "RHEL-777" in result.summary
    assert ("get_jira_details", {"issue_key": "RHEL-777"}) in calls
    assert "add_merge_request_labels" not in [name for name, _ in calls]


@pytest.mark.asyncio
async def test_no_linked_issues_bail_skips_labeling_in_gating_mode(monkeypatch):
    """Gating mode never touches MR labels -- that lifecycle belongs to anewa alone."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append(name)
        if name == "get_merge_request_commit_messages":
            return []
        if name == "get_merge_request_details":
            return {"description": "Just a plain MR with no linked issue."}
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1", gating=True)

    assert result.analyzed is False
    assert calls == ["get_merge_request_details", "get_merge_request_commit_messages"]


@pytest.mark.asyncio
async def test_jira_fetch_failure_aborts_without_posting_or_labeling(monkeypatch):
    """A Jira outage must not be swallowed into an empty CVE/Test Coverage
    context and allowed to proceed -- that risks misclassifying an issue as
    untested and marking the MR analyzed on incomplete data. The whole run
    aborts instead, with nothing posted or labeled, so it can be retried."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append(name)
        if name == "get_merge_request_commit_messages":
            return []
        if name == "get_merge_request_details":
            return {"description": "Resolves: RHEL-111"}
        if name == "get_authorized_comments_from_merge_request":
            return {"comments": []}
        if name == "fetch_gitlab_mr_notes":
            return "[]"
        if name == "parse_mr_test_results":
            return dict(_EMPTY_PARSE_MR_TEST_RESULTS)
        if name == "parse_newa_mr_comment":
            return dict(_NOT_FOUND_PARSE_NEWA)
        if name == "get_jira_details":
            raise RuntimeError("Jira is down")
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is False
    assert "Jira is down" in result.summary
    assert "add_merge_request_labels" not in calls
    assert "add_merge_request_comment" not in calls


@pytest.mark.asyncio
async def test_linked_issues_gather_jira_context_and_prompt_has_no_posting_knobs(monkeypatch):
    """Linked issues are resolved to CVE id / Test Coverage. dry_run/poc/
    no_jira_comments must NOT appear in the agent's prompt -- those are
    posting-time concerns handled entirely by act_on_result, never something
    the agent's own analysis needs to reason about. The raw MR comments are
    parsed deterministically in Python (parse_mr_results) and only the
    already-parsed PARSED_RESULTS JSON is appended to the prompt -- never the
    raw comments."""
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

    parsed_results = {
        "nvr": "foo-1.0",
        "results_by_arch": {"x86_64": {"overall_status": "✅ passed"}},
        "unfixed_results_by_arch": {},
        "note_id": 7,
        "note_body": "**ANEWA:** testing completed.",
        "report_portal_url": None,
    }

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        if name == "get_merge_request_commit_messages":
            return []
        if name == "get_merge_request_details":
            return {"description": "Resolves: RHEL-111\nResolves: RHEL-222"}
        if name == "get_authorized_comments_from_merge_request":
            return {
                "comments": [{"body": "**ANEWA:** testing completed.", "created_at": "2024-01-01T00:00:00Z"}]
            }
        if name == "parse_mr_test_results":
            assert kwargs["comments"] == [
                {"body": "**ANEWA:** testing completed.", "created_at": "2024-01-01T00:00:00Z"}
            ]
            return parsed_results
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
                MrTestingAnalystResult(analyzed=True, summary="built everything").model_dump_json()
            )

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(m).should_receive("create_mr_testing_analyst_agent").and_return(_FakeAgent())
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst(
        "https://gitlab.com/x/y/-/merge_requests/1",
        dry_run=True,
        poc=True,
        no_jira_comments=True,
    )

    assert result.analyzed is True
    assert result.summary == "built everything"

    prompt = captured_prompt["prompt"]
    assert "RHEL-111" in prompt
    assert "CVE-2024-12345" in prompt
    assert "Automated" in prompt
    assert "RHEL-222" in prompt
    assert "dry_run" not in prompt
    assert "poc:" not in prompt
    assert "no_jira_comments" not in prompt
    # Raw MR comments are no longer appended -- only the already-parsed result.
    assert "MR_COMMENTS" not in prompt
    parsed_json = re.search(r"^\{.*\}$", prompt, re.MULTILINE).group(0)
    assert json.loads(parsed_json) == parsed_results


@pytest.mark.asyncio
async def test_fetch_test_results_falls_back_to_mr_notes_on_exception(monkeypatch):
    """If get_authorized_comments_from_merge_request fails, fall back to
    fetch_gitlab_mr_notes -- which (unlike every other MR tool used here)
    takes project/mr_iid rather than merge_request_url, and returns its
    result as a JSON string body rather than structured JSON. Both quirks
    must be handled, or the fallback silently produces an empty comment list."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append((name, kwargs))
        if name == "get_merge_request_commit_messages":
            return []
        if name == "get_merge_request_details":
            return {"description": "Resolves: RHEL-333"}
        if name == "get_authorized_comments_from_merge_request":
            raise RuntimeError("boom")
        if name == "fetch_gitlab_mr_notes":
            assert kwargs == {"project": "x/y", "mr_iid": 1}
            # The real tool returns StringToolOutput -- a JSON string, not
            # structured data -- so the fallback must json.loads() it itself.
            return json.dumps([{"author": "anewa", "body": "**ANEWA:** done", "created_at": "t"}])
        if name == "parse_mr_test_results":
            assert kwargs["comments"] == [{"author": "anewa", "body": "**ANEWA:** done", "created_at": "t"}]
            return dict(_EMPTY_PARSE_MR_TEST_RESULTS)
        if name == "parse_newa_mr_comment":
            return dict(_NOT_FOUND_PARSE_NEWA)
        if name == "get_jira_details":
            return {"fields": {"summary": "", "labels": [], "customfield_10638": None}}
        if name in ("add_merge_request_labels", "remove_merge_request_labels"):
            return "ok"
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(m).should_receive("create_mr_testing_analyst_agent").and_return(
        _fake_agent(MrTestingAnalystResult(analyzed=True, summary="done"))
    )
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is True
    call_names = [name for name, _ in calls]
    assert "get_authorized_comments_from_merge_request" in call_names
    assert "fetch_gitlab_mr_notes" in call_names


@pytest.mark.asyncio
async def test_fetch_test_results_falls_back_when_successful_result_lacks_marker(monkeypatch):
    """get_authorized_comments_from_merge_request filters to Developer-role-
    or-higher MR members -- if the results bot isn't one, its note can be
    silently absent even from a *successful* response. That must also
    trigger the fetch_gitlab_mr_notes fallback, not just an outright
    exception."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append((name, kwargs))
        if name == "get_merge_request_commit_messages":
            return []
        if name == "get_merge_request_details":
            return {"description": "Resolves: RHEL-444"}
        if name == "get_authorized_comments_from_merge_request":
            # Successful call, but the bot's own note was filtered out.
            return {"comments": [{"body": "unrelated human comment", "created_at": "t0"}]}
        if name == "fetch_gitlab_mr_notes":
            assert kwargs == {"project": "x/y", "mr_iid": 1}
            return json.dumps(
                [
                    {"body": "unrelated human comment", "created_at": "t0"},
                    {"body": "**ANEWA:** done", "created_at": "t1", "note_id": 9},
                ]
            )
        if name == "parse_mr_test_results":
            assert len(kwargs["comments"]) == 2
            return dict(_EMPTY_PARSE_MR_TEST_RESULTS)
        if name == "parse_newa_mr_comment":
            return dict(_NOT_FOUND_PARSE_NEWA)
        if name == "get_jira_details":
            return {"fields": {"summary": "", "labels": [], "customfield_10638": None}}
        if name in ("add_merge_request_labels", "remove_merge_request_labels"):
            return "ok"
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(m).should_receive("create_mr_testing_analyst_agent").and_return(
        _fake_agent(MrTestingAnalystResult(analyzed=True, summary="done"))
    )
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is True
    call_names = [name for name, _ in calls]
    assert "get_authorized_comments_from_merge_request" in call_names
    assert "fetch_gitlab_mr_notes" in call_names


@pytest.mark.asyncio
async def test_fetch_test_results_no_fallback_when_marker_already_present(monkeypatch):
    """If get_authorized_comments_from_merge_request's own result already
    contains the expected marker, don't bother falling back at all."""
    calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append((name, kwargs))
        if name == "get_merge_request_commit_messages":
            return []
        if name == "get_merge_request_details":
            return {"description": "Resolves: RHEL-555"}
        if name == "get_authorized_comments_from_merge_request":
            return {"comments": [{"body": "**ANEWA:** done", "created_at": "t0"}]}
        if name == "parse_mr_test_results":
            return dict(_EMPTY_PARSE_MR_TEST_RESULTS)
        if name == "parse_newa_mr_comment":
            return dict(_NOT_FOUND_PARSE_NEWA)
        if name == "get_jira_details":
            return {"fields": {"summary": "", "labels": [], "customfield_10638": None}}
        if name in ("add_merge_request_labels", "remove_merge_request_labels"):
            return "ok"
        raise AssertionError(f"Unexpected tool call: {name}")

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(m).should_receive("create_mr_testing_analyst_agent").and_return(
        _fake_agent(MrTestingAnalystResult(analyzed=True, summary="done"))
    )
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    result = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1")

    assert result.analyzed is True
    call_names = [name for name, _ in calls]
    assert "get_authorized_comments_from_merge_request" in call_names
    assert "fetch_gitlab_mr_notes" not in call_names


# ---------------------------------------------------------------------------
# act_on_result: deterministic posting
# ---------------------------------------------------------------------------


async def _run_with_result(
    monkeypatch,
    result: MrTestingAnalystResult,
    parse_overrides: dict | None = None,
    discussions: list | None = None,
    raise_on: dict | None = None,
    **run_kwargs,
):
    """Run the full workflow with a minimal happy-path fetch/Jira mock and a
    fake ReasoningAgent that returns `result`, recording every run_tool call."""
    calls = []
    parse_overrides = parse_overrides or {}
    raise_on = raise_on or {}

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        calls.append((name, kwargs))
        if name in raise_on:
            raise raise_on[name]
        if name == "get_merge_request_commit_messages":
            return []
        if name == "get_merge_request_details":
            return {"description": "Resolves: RHEL-111"}
        if name == "get_authorized_comments_from_merge_request":
            return {"comments": []}
        if name == "fetch_gitlab_mr_notes":
            return "[]"
        if name == "parse_mr_test_results":
            return parse_overrides.get("parse_mr_test_results", dict(_EMPTY_PARSE_MR_TEST_RESULTS))
        if name == "parse_newa_mr_comment":
            return parse_overrides.get("parse_newa_mr_comment", dict(_NOT_FOUND_PARSE_NEWA))
        if name == "parse_osci_results_comment":
            return parse_overrides.get("parse_osci_results_comment", dict(_DEFAULT_PARSE_OSCI))
        if name == "get_merge_request_discussions":
            return discussions if discussions is not None else []
        if name == "get_jira_details":
            return {"fields": {"summary": "", "labels": [], "customfield_10638": None}}
        # Every posting tool: just acknowledge the call.
        return "ok"

    flexmock(m).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(m).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(m).should_receive("create_mr_testing_analyst_agent").and_return(_fake_agent(result))

    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")
    final = await run_mr_testing_analyst("https://gitlab.com/x/y/-/merge_requests/1", **run_kwargs)
    return final, calls


@pytest.mark.asyncio
async def test_act_on_result_posts_new_comments_and_jira_comments_and_labels(monkeypatch):
    result = MrTestingAnalystResult(
        analyzed=True,
        summary="done",
        mr_comments=[MrCommentToPost(kind="new", body="## Testing Farm Results")],
        jira_comments=[{"jira_key": "RHEL-111", "body": "## Testing Results"}],
    )

    _, calls = await _run_with_result(monkeypatch, result)
    call_names = [name for name, _ in calls]

    assert "add_merge_request_comment" in call_names
    assert "add_jira_comment" in call_names
    jira_call = next(kwargs for name, kwargs in calls if name == "add_jira_comment")
    assert jira_call == {"issue_key": "RHEL-111", "comment": "## Testing Results", "private": True}
    assert "add_merge_request_labels" in call_names
    assert "remove_merge_request_labels" in call_names


@pytest.mark.asyncio
async def test_act_on_result_update_kind_folds_into_anewas_note(monkeypatch):
    """The model never folds this itself (fold_mr_comment_body isn't in its
    toolset) -- Python folds the model's plain formatted markdown into
    anewa's own note_body (from parse_mr_test_results) using
    fold_comment_body directly."""
    note_body = "**ANEWA:** done\n**Build:** `foo-1.0`"
    result = MrTestingAnalystResult(
        analyzed=True,
        summary="done",
        mr_comments=[MrCommentToPost(kind="update", body="## Testing Farm Results\nnew", note_id=42)],
    )
    overrides = {
        "parse_mr_test_results": {
            "nvr": "foo-1.0",
            "results_by_arch": {"x86_64": {"overall_status": "✅ pass"}},
            "unfixed_results_by_arch": {},
            "note_id": 42,
            "note_body": note_body,
            "report_portal_url": None,
        }
    }

    _, calls = await _run_with_result(monkeypatch, result, parse_overrides=overrides)
    update_call = next(kwargs for name, kwargs in calls if name == "update_merge_request_comment")

    assert update_call["note_id"] == 42
    assert update_call["comment"] == fold_comment_body(note_body, "## Testing Farm Results\nnew")
    assert all(name != "add_merge_request_comment" for name, _ in calls)


@pytest.mark.asyncio
async def test_act_on_result_update_kind_without_note_id_falls_back_to_new(monkeypatch):
    result = MrTestingAnalystResult(
        analyzed=True,
        summary="done",
        mr_comments=[MrCommentToPost(kind="update", body="merged body", note_id=None)],
    )

    _, calls = await _run_with_result(monkeypatch, result)
    call_names = [name for name, _ in calls]
    assert "add_merge_request_comment" in call_names
    assert "update_merge_request_comment" not in call_names


@pytest.mark.asyncio
async def test_act_on_result_blocking_kind_uses_discussion_unless_poc(monkeypatch):
    result = MrTestingAnalystResult(
        analyzed=True,
        summary="done",
        mr_comments=[MrCommentToPost(kind="blocking", body="untested issues")],
    )

    _, calls = await _run_with_result(monkeypatch, result)
    call_names = [name for name, _ in calls]
    assert "add_blocking_merge_request_comment" in call_names
    assert "add_merge_request_comment" not in call_names

    _, poc_calls = await _run_with_result(monkeypatch, result, poc=True)
    poc_call_names = [name for name, _ in poc_calls]
    assert "add_merge_request_comment" in poc_call_names
    assert "add_blocking_merge_request_comment" not in poc_call_names


@pytest.mark.asyncio
async def test_act_on_result_resolves_matching_osci_discussion(monkeypatch):
    """The model only returns a boolean (resolve_osci_discussion) -- Python
    independently finds the right discussion to resolve, matching the
    results-comment marker, resolvable/unresolved state, and this run's own
    pipeline ID (from parse_osci_results_comment)."""
    result = MrTestingAnalystResult(analyzed=True, summary="done", resolve_osci_discussion=True)
    discussions = [
        {
            "discussion_id": "d1",
            "resolvable": True,
            "resolved": False,
            "body": "<!--RESULTS_COMMENT-->Results for pipeline [123](https://x)",
            "created_at": "2024-01-01T00:00:00Z",
        },
        {
            "discussion_id": "d2",
            "resolvable": False,
            "resolved": None,
            "body": "unrelated",
            "created_at": "2024-01-01T00:00:01Z",
        },
    ]

    _, calls = await _run_with_result(monkeypatch, result, gating=True, discussions=discussions)
    resolve_call = next(kwargs for name, kwargs in calls if name == "resolve_merge_request_discussion")
    assert resolve_call["discussion_id"] == "d1"
    # Gating mode never labels the MR, even though analyzed is true.
    assert all(name != "add_merge_request_labels" for name, _ in calls)


@pytest.mark.asyncio
async def test_act_on_result_resolve_osci_discussion_no_match_skips_without_raising(monkeypatch):
    """If resolve_osci_discussion is set but nothing actually matches (wrong
    pipeline ID, already resolved, or no results-comment thread at all),
    this must skip resolving -- never resolve blind -- and must not raise."""
    result = MrTestingAnalystResult(analyzed=True, summary="done", resolve_osci_discussion=True)
    discussions = [
        {
            "discussion_id": "d1",
            "resolvable": True,
            "resolved": False,
            "body": "<!--RESULTS_COMMENT-->Results for pipeline [999](https://x)",
            "created_at": "2024-01-01T00:00:00Z",
        },
    ]

    _, calls = await _run_with_result(monkeypatch, result, gating=True, discussions=discussions)
    call_names = [name for name, _ in calls]
    assert "resolve_merge_request_discussion" not in call_names


@pytest.mark.asyncio
async def test_act_on_result_gating_never_labels_mr(monkeypatch):
    result = MrTestingAnalystResult(analyzed=True, summary="done")

    _, calls = await _run_with_result(monkeypatch, result, gating=True)
    call_names = [name for name, _ in calls]
    assert "add_merge_request_labels" not in call_names
    assert "remove_merge_request_labels" not in call_names


@pytest.mark.asyncio
async def test_act_on_result_dry_run_posts_nothing(monkeypatch):
    result = MrTestingAnalystResult(
        analyzed=True,
        summary="done",
        mr_comments=[MrCommentToPost(kind="new", body="x")],
        jira_comments=[{"jira_key": "RHEL-111", "body": "y"}],
        resolve_osci_discussion=True,
    )

    _, calls = await _run_with_result(monkeypatch, result, dry_run=True)
    posting_tool_names = {
        "add_merge_request_comment",
        "update_merge_request_comment",
        "add_blocking_merge_request_comment",
        "add_merge_request_labels",
        "remove_merge_request_labels",
        "add_jira_comment",
        "resolve_merge_request_discussion",
    }
    assert not any(name in posting_tool_names for name, _ in calls)


@pytest.mark.asyncio
async def test_act_on_result_no_jira_comments_skips_jira_posting_only(monkeypatch):
    result = MrTestingAnalystResult(
        analyzed=True,
        summary="done",
        mr_comments=[MrCommentToPost(kind="new", body="x")],
        jira_comments=[{"jira_key": "RHEL-111", "body": "y"}],
    )

    _, calls = await _run_with_result(monkeypatch, result, no_jira_comments=True)
    call_names = [name for name, _ in calls]
    assert "add_jira_comment" not in call_names
    assert "add_merge_request_comment" in call_names
    assert "add_merge_request_labels" in call_names


@pytest.mark.asyncio
async def test_act_on_result_jira_failure_does_not_block_labeling(monkeypatch):
    """A Jira-comment-posting failure must not prevent the MR from being
    labeled once its own mr_comments have already posted successfully --
    otherwise a retry would repost every mr_comment from scratch, risking
    duplicates, just because of an unrelated Jira outage."""
    result = MrTestingAnalystResult(
        analyzed=True,
        summary="done",
        mr_comments=[MrCommentToPost(kind="new", body="x")],
        jira_comments=[{"jira_key": "RHEL-111", "body": "y"}],
    )

    final, calls = await _run_with_result(
        monkeypatch, result, raise_on={"add_jira_comment": RuntimeError("Jira is down")}
    )
    call_names = [name for name, _ in calls]

    assert final.analyzed is True
    assert "add_merge_request_comment" in call_names
    assert "add_jira_comment" in call_names
    assert "add_merge_request_labels" in call_names
    assert "remove_merge_request_labels" in call_names


class _FakeRedis:
    def __init__(self):
        self.pushed: list[tuple[str, str]] = []
        self._counter = 0

    def incr(self, _key):
        self._counter += 1
        return self._counter

    def lpush(self, key, value):
        self.pushed.append((key, value))
        return len(self.pushed)


async def _run_queue_mode(monkeypatch, payloads, run_fn, max_retries="2"):
    """Run main() in queue mode, feeding `payloads` to its process_task."""
    fake_redis = _FakeRedis()

    @contextlib.asynccontextmanager
    async def _fake_redis_client(_url):
        yield fake_redis

    async def _fake_run_task_loop(redis, queues, process_fn, **kwargs):
        assert queues == ["mr_testing_queue"]
        for payload in payloads:
            await process_fn(payload)

    async def _fake_fix_await(value):
        return value

    span = flexmock(start_transaction=lambda *a, **k: contextlib.nullcontext())
    flexmock(m).should_receive("init_sentry")
    flexmock(m).should_receive("configure_logging")
    flexmock(m).should_receive("setup_observability").and_return(span)
    flexmock(m).should_receive("install_shutdown_handler")
    flexmock(m).should_receive("redis_client").replace_with(_fake_redis_client)
    flexmock(m).should_receive("run_task_loop").replace_with(_fake_run_task_loop)
    flexmock(m).should_receive("fix_await").replace_with(_fake_fix_await)
    flexmock(m).should_receive("run_mr_testing_analyst").replace_with(run_fn)
    monkeypatch.delenv("MR_URL", raising=False)
    monkeypatch.setenv("COLLECTOR_ENDPOINT", "http://collector.invalid")
    monkeypatch.setenv("REDIS_URL", "redis://invalid")
    monkeypatch.setenv("MAX_RETRIES", max_retries)

    await m.main()
    return fake_redis


@pytest.mark.asyncio
async def test_queue_mode_runs_task_with_metadata(monkeypatch):
    calls = []

    async def _run(mr_url, **kwargs):
        calls.append((mr_url, kwargs))
        return MrTestingAnalystResult(analyzed=True, summary="ok")

    task = Task(metadata={"mr_url": "https://gitlab.com/x/y/-/merge_requests/1", "gating": True})
    redis = await _run_queue_mode(monkeypatch, [task.model_dump_json()], _run)

    assert calls == [
        (
            "https://gitlab.com/x/y/-/merge_requests/1",
            {"dry_run": False, "poc": False, "no_jira_comments": False, "gating": True},
        )
    ]
    assert redis.pushed == []


@pytest.mark.asyncio
async def test_queue_mode_retries_then_moves_to_error_list(monkeypatch):
    async def _run(mr_url, **kwargs):
        raise RuntimeError("boom")

    task = Task(metadata={"mr_url": "https://gitlab.com/x/y/-/merge_requests/1"})
    redis = await _run_queue_mode(monkeypatch, [task.model_dump_json()], _run, max_retries="2")

    # attempts 0 -> 1: requeued; the requeued payload is not fed back by the fake loop
    assert [key for key, _ in redis.pushed] == ["mr_testing_queue"]
    requeued = Task.model_validate_json(redis.pushed[0][1])
    assert requeued.attempts == 1

    # Final attempt: moved to the error list with the task kept for requeueing
    redis = await _run_queue_mode(monkeypatch, [requeued.model_dump_json()], _run, max_retries="2")
    assert [key for key, _ in redis.pushed] == ["error_list"]
    entry = ErrorListEntry.model_validate_json(redis.pushed[0][1])
    assert entry.queue == "mr_testing_queue"
    assert entry.task is not None
    assert "boom" in entry.error.details


@pytest.mark.asyncio
async def test_queue_mode_malformed_metadata_goes_to_error_list(monkeypatch):
    async def _run(mr_url, **kwargs):
        raise AssertionError("must not run")

    task = Task(metadata={"not_mr_url": 1})
    redis = await _run_queue_mode(monkeypatch, [task.model_dump_json(), b"not json"], _run)

    entries = [ErrorListEntry.model_validate_json(value) for key, value in redis.pushed]
    assert [key for key, _ in redis.pushed] == ["error_list", "error_list"]
    assert entries[0].task is not None
    assert "Malformed task metadata" in entries[0].error.details
    assert entries[1].task is None
    assert "Malformed task payload" in entries[1].error.details
