"""Build info (Copr link, modular notes) is posted as an MR comment, not in Jira.

The build step captures `BuildOutputSchema.build_url` into `state.build_url`.
After the Jira comment is posted, if a merge request URL exists and there is a
build URL (or the issue is modular), a separate comment is added to the MR via
`tasks.comment_in_mr` so reviewers see the build context where they review.

`fix_build_error` also propagates `build_url` from the fix agent's
`BackportOutputSchema` when one is reported, so the MR comment can link to the
latest build rather than only the original failed one.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import backport_agent, tasks
from ymir.common.models import BackportOutputSchema


async def _run_comment_in_jira(
    monkeypatch,
    *,
    build_url,
    merge_request_url="https://gitlab.com/x/y/-/merge_requests/1",
    is_modular_issue=False,
    module_stream=None,
):
    """Start the real workflow at comment_in_jira with a successful backport result."""
    captured_jira_comments = []
    captured_mr_comments = []

    async def _fake_comment_in_jira(*, comment_text, **_kwargs):
        captured_jira_comments.append(comment_text)

    async def _fake_comment_in_mr(*, merge_request_url, comment_text, **_kwargs):
        captured_mr_comments.append(comment_text)

    flexmock(tasks).should_receive("comment_in_jira").replace_with(_fake_comment_in_jira).once()
    flexmock(tasks).should_receive("comment_in_mr").replace_with(_fake_comment_in_mr).at_most().once()

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
        is_modular_issue=is_modular_issue,
        module_stream=module_stream,
    )

    assert len(captured_jira_comments) == 1
    return captured_jira_comments[0], captured_mr_comments


@pytest.mark.asyncio
async def test_build_url_posted_on_mr_not_jira(monkeypatch):
    build_url = "https://copr.devel.redhat.com/coprs/jotnar-bot/RHEL-999/build/166632/"
    jira_text, mr_comments = await _run_comment_in_jira(monkeypatch, build_url=build_url)

    assert "Build:" not in jira_text
    assert jira_text == "https://gitlab.com/x/y/-/merge_requests/1"

    assert len(mr_comments) == 1
    assert build_url in mr_comments[0]
    assert "Copr projects are temporary" in mr_comments[0]


@pytest.mark.asyncio
async def test_no_mr_comment_when_no_build_url(monkeypatch):
    """No build URL and not modular → no MR comment at all."""
    jira_text, mr_comments = await _run_comment_in_jira(monkeypatch, build_url=None)

    assert jira_text == "https://gitlab.com/x/y/-/merge_requests/1"
    assert len(mr_comments) == 0


@pytest.mark.asyncio
async def test_modular_note_posted_on_mr(monkeypatch):
    build_url = "https://copr.devel.redhat.com/coprs/jotnar-bot/RHEL-999/build/166632/"
    jira_text, mr_comments = await _run_comment_in_jira(
        monkeypatch,
        build_url=build_url,
        is_modular_issue=True,
        module_stream="postgresql:16",
    )

    assert "Modular" not in jira_text
    assert len(mr_comments) == 1
    assert "postgresql:16" in mr_comments[0]
    assert build_url in mr_comments[0]


@pytest.mark.asyncio
async def test_fix_build_error_propagates_build_url(monkeypatch, tmp_path):
    """When the LLM fix-agent succeeds and reports a build_url in its output,
    fix_build_error must capture it into state.build_url so the MR comment
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
