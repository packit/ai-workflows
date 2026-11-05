"""Build info (Copr link, modular notes) belongs in the MR description, not Jira.

The build step captures `BuildOutputSchema.build_url` into `state.build_url`.
When the MR is opened, that URL (plus any modular notes) is appended to the MR
description via `build_mr_note`. The Jira comment stays a plain MR link.

`fix_build_error` also propagates `build_url` from the fix agent's
`BackportOutputSchema` when one is reported, so the MR description can link to
the latest build rather than only the original failed one.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import backport_agent, tasks
from ymir.agents.package_update_steps import build_mr_note
from ymir.common.models import BackportOutputSchema, LogOutputSchema


def test_build_mr_note_includes_build_url_and_expiry_warning():
    note = build_mr_note(
        build_url="https://copr.example/build/1/",
        is_modular_issue=False,
        module_stream=None,
    )
    assert "https://copr.example/build/1/" in note
    assert "Copr projects are temporary" in note


def test_build_mr_note_includes_modular_stream():
    note = build_mr_note(
        build_url="https://copr.example/build/1/",
        is_modular_issue=True,
        module_stream="postgresql:16",
    )
    assert "postgresql:16" in note
    assert "https://copr.example/build/1/" in note


def test_build_mr_note_empty_when_nothing_to_report():
    assert build_mr_note(build_url=None, is_modular_issue=False, module_stream=None) == ""


async def _run_comment_in_jira(
    monkeypatch,
    *,
    build_url,
    merge_request_url="https://gitlab.com/x/y/-/merge_requests/1",
):
    """Start the real workflow at comment_in_jira with a successful backport result."""
    captured_jira_comments = []

    async def _fake_comment_in_jira(*, comment_text, **_kwargs):
        captured_jira_comments.append(comment_text)

    flexmock(tasks).should_receive("comment_in_jira").replace_with(_fake_comment_in_jira).once()
    flexmock(tasks).should_receive("comment_in_mr").never()

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway)
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None)
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    run_workflow = Workflow.run

    def start_at_comment(workflow, state, options=None):
        if workflow.name == "BackportWorkflow":
            state.backport_result = BackportOutputSchema(
                success=True, status="done", srpm_path=None, error=None
            )
            state.merge_request_url = merge_request_url
            state.build_url = build_url
            workflow.set_start("comment_in_jira")
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_comment)

    await backport_agent.run_workflow(
        package="postgresql",
        dist_git_branch="c10s",
        upstream_patches=[],
        jira_issue="RHEL-999",
        cve_id=None,
        dry_run=False,
        backport_agent_factory=lambda *_: None,
        is_modular_issue=False,
        module_stream=None,
    )

    assert len(captured_jira_comments) == 1
    return captured_jira_comments[0]


@pytest.mark.asyncio
async def test_jira_comment_is_plain_mr_link_without_build_url(monkeypatch):
    build_url = "https://copr.devel.redhat.com/coprs/jotnar-bot/RHEL-999/build/166632/"
    jira_text = await _run_comment_in_jira(monkeypatch, build_url=build_url)

    assert jira_text == "https://gitlab.com/x/y/-/merge_requests/1"
    assert "Build:" not in jira_text
    assert build_url not in jira_text


@pytest.mark.asyncio
async def test_mr_description_includes_build_url(monkeypatch, tmp_path):
    """commit_push_and_open_mr embeds build_mr_note into the MR description."""
    build_url = "https://copr.devel.redhat.com/coprs/jotnar-bot/RHEL-999/build/166632/"
    local_clone = tmp_path / "pkg"
    local_clone.mkdir()
    captured = {}

    async def _fake_commit_push_and_open_mr(**kwargs):
        captured["mr_description"] = kwargs["mr_description"]
        return "https://gitlab.com/x/y/-/merge_requests/1", True

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway)
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None)
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None)

    async def _fake_needs_zstream_target_label(*_a, **_kw):
        return False

    flexmock(tasks).should_receive("commit_push_and_open_mr").replace_with(
        _fake_commit_push_and_open_mr
    ).once()
    flexmock(tasks).should_receive("needs_zstream_target_label").replace_with(
        _fake_needs_zstream_target_label
    )
    flexmock(tasks).should_receive("comment_in_mr").never()
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    run_workflow = Workflow.run

    def start_at_open_mr(workflow, state, options=None):
        if workflow.name == "BackportWorkflow":
            state.backport_result = BackportOutputSchema(
                success=True, status="done", srpm_path=None, error=None
            )
            state.local_clone = local_clone
            state.fork_url = "https://gitlab.com/fork/pkg.git"
            state.update_branch = "ymir-RHEL-999"
            state.build_url = build_url
            state.backport_log = ["cherry-picked"]
            state.log_result = LogOutputSchema(title="Fix something", description="Details")
            state.upstream_patches = ["https://example.com/patch.diff"]
            workflow.set_start("commit_push_and_open_mr")

            async def skip_rest(_state):
                return Workflow.END

            workflow.steps["submit_consolidation_job"].handler = skip_rest
            workflow.steps["comment_in_jira"].handler = skip_rest
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_open_mr)

    await backport_agent.run_workflow(
        package="postgresql",
        dist_git_branch="c10s",
        upstream_patches=["https://example.com/patch.diff"],
        jira_issue="RHEL-999",
        cve_id=None,
        dry_run=False,
        backport_agent_factory=lambda *_: None,
        is_modular_issue=True,
        module_stream="postgresql:16",
    )

    description = captured["mr_description"]
    assert build_url in description
    assert "Copr projects are temporary" in description
    assert "postgresql:16" in description


@pytest.mark.asyncio
async def test_fix_build_error_propagates_build_url(monkeypatch, tmp_path):
    """When the LLM fix-agent succeeds and reports a build_url in its output,
    fix_build_error must capture it into state.build_url so the MR description
    links to the successful retry — not the original failed build."""
    fix_build_url = "https://copr.devel.redhat.com/coprs/jotnar-bot/RHEL-999/build/200000/"
    old_build_url = "https://copr.devel.redhat.com/coprs/jotnar-bot/RHEL-999/build/199999/"

    local_clone = tmp_path / "pkg"
    local_clone.mkdir()
    unpacked_sources = tmp_path / "pkg-upstream"
    unpacked_sources.mkdir()

    fix_output = BackportOutputSchema(
        success=True,
        status="Fixed build by cherry-picking prerequisite",
        srpm_path=Path("/tmp/test.src.rpm"),
        error=None,
        build_url=fix_build_url,
    )

    mock_response = SimpleNamespace(
        last_message=SimpleNamespace(text=fix_output.model_dump_json()),
    )

    async def _fake_run(*_a, **_kw):
        return mock_response

    async def _fake_create_backport_agent(*_a, **_kw):
        agent = flexmock()
        agent.should_receive("run").replace_with(_fake_run).once()
        return agent

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway)
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None)
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None)
    flexmock(backport_agent).should_receive("create_backport_agent").replace_with(_fake_create_backport_agent)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    captured_build_url = {}

    run_workflow = Workflow.run

    def start_at_fix_build(workflow, state, options=None):
        if workflow.name == "BackportWorkflow":
            state.backport_result = BackportOutputSchema(
                success=False, status="", srpm_path=Path("/tmp/test.src.rpm"), error="build failed"
            )
            state.build_error = "build failed"
            state.build_url = old_build_url
            state.used_cherry_pick_workflow = True
            state.local_clone = local_clone
            state.unpacked_sources = unpacked_sources
            state.incremental_fix_attempts = 0
            workflow.set_start("fix_build_error")

            async def capture_and_end(s):
                captured_build_url["url"] = s.build_url
                return Workflow.END

            workflow.steps["update_release"].handler = capture_and_end
            workflow.steps["comment_in_jira"].handler = lambda _: Workflow.END
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_fix_build)

    await backport_agent.run_workflow(
        package="postgresql",
        dist_git_branch="c10s",
        upstream_patches=[],
        jira_issue="RHEL-999",
        cve_id=None,
        dry_run=False,
        backport_agent_factory=lambda *_: None,
        is_modular_issue=False,
        module_stream=None,
    )

    assert captured_build_url["url"] == fix_build_url


@pytest.mark.asyncio
async def test_fix_build_error_keeps_old_url_when_no_build_was_attempted(monkeypatch, tmp_path):
    """If the fix agent reports no build_url (e.g. patch regeneration failed),
    state.build_url keeps the URL captured from the previous build step."""
    old_build_url = "https://copr.devel.redhat.com/coprs/jotnar-bot/RHEL-999/build/199999/"

    local_clone = tmp_path / "pkg"
    local_clone.mkdir()
    unpacked_sources = tmp_path / "pkg-upstream"
    unpacked_sources.mkdir()

    fix_output = BackportOutputSchema(
        success=False,
        status="",
        srpm_path=None,
        error="Could not regenerate patch",
        build_url=None,
    )

    mock_response = SimpleNamespace(
        last_message=SimpleNamespace(text=fix_output.model_dump_json()),
    )

    async def _fake_run(*_a, **_kw):
        return mock_response

    async def _fake_create_backport_agent(*_a, **_kw):
        agent = flexmock()
        agent.should_receive("run").replace_with(_fake_run).once()
        return agent

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway)
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None)
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None)
    flexmock(backport_agent).should_receive("create_backport_agent").replace_with(_fake_create_backport_agent)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    captured_build_url = {}

    run_workflow = Workflow.run

    def start_at_fix_build(workflow, state, options=None):
        if workflow.name == "BackportWorkflow":
            state.backport_result = BackportOutputSchema(
                success=False, status="", srpm_path=Path("/tmp/test.src.rpm"), error="build failed"
            )
            state.build_error = "build failed"
            state.build_url = old_build_url
            state.used_cherry_pick_workflow = True
            state.local_clone = local_clone
            state.unpacked_sources = unpacked_sources
            state.incremental_fix_attempts = 0
            state.attempts_remaining = 0
            workflow.set_start("fix_build_error")

            async def capture_and_end(s):
                captured_build_url["url"] = s.build_url
                return Workflow.END

            workflow.steps["comment_in_jira"].handler = capture_and_end
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_fix_build)

    await backport_agent.run_workflow(
        package="postgresql",
        dist_git_branch="c10s",
        upstream_patches=[],
        jira_issue="RHEL-999",
        cve_id=None,
        dry_run=False,
        backport_agent_factory=lambda *_: None,
        is_modular_issue=False,
        module_stream=None,
        max_incremental_fix_attempts=1,
    )

    assert captured_build_url["url"] == old_build_url
