"""Build-repair diagnostics must not become part of generated source patches."""

import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import backport_agent
from ymir.common.models import BackportOutputSchema, BuildOutputSchema
from ymir.tools.unprivileged.wicked_git import GitPatchCreationTool


@pytest.mark.parametrize("branch", ["c9s", "c10s"])
@pytest.mark.asyncio
async def test_build_repair_logs_stay_out_of_git(monkeypatch, tmp_path, branch):
    local_clone = tmp_path / "expat"
    upstream = tmp_path / "expat-upstream"
    log_dir = tmp_path / "expat-build-logs"

    def git(repo, *args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    for repo, filename in ((local_clone, "expat.spec"), (upstream, "source.c")):
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Ymir Tests")
        git(repo, "config", "user.email", "ymir-tests@example.com")
        (repo / filename).write_text("initial content\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "Initial content")

    base = git(upstream, "rev-parse", "HEAD")
    patch_file = local_clone / "fix.patch"
    patch_tool = GitPatchCreationTool(options={"base_head_commit": base})
    (local_clone / "builder-live.log").write_text("initial build log")
    (local_clone / "root.log.gz").write_bytes(b"initial compressed log")
    attempts = []

    async def repair(prompt, **kwargs):
        attempt = len(attempts) + 1
        attempts.append(prompt)
        notes = log_dir / "fix-attempts.md"
        assert notes.exists(), "Repair notes must be outside both Git checkouts"
        assert str(notes) in prompt
        assert notes.read_text() not in prompt, "Saved history should be read from the file as needed"
        assert str(upstream / "build-logs") not in prompt
        assert f"## Attempt {attempt}" in notes.read_text()
        assert (log_dir / "attempt-0" / "builder-live.log").read_text() == "initial build log"
        assert (log_dir / "attempt-0" / "root.log.gz").read_bytes() == b"initial compressed log"
        if attempt == 2:
            assert "First repair summary" in notes.read_text()
            assert "First repair summary" not in prompt
            assert "second build failure" in notes.read_text()
            assert (log_dir / "attempt-1" / "builder-live.log").read_text() == "retry build log"

        # Repeat the blanket staging that included fix-attempts.md in production.
        (upstream / "source.c").write_text(f"int value = {attempt};\n")
        git(upstream, "add", "-A")
        git(upstream, "commit", "-m", f"Repair {attempt}")
        await patch_tool.run({"repository_path": upstream, "patch_file_path": patch_file})
        assert git(upstream, "diff", "--name-only", base, "HEAD") == "source.c"
        assert "fix-attempts.md" not in patch_file.read_text()
        assert "build-logs" not in patch_file.read_text()
        assert f"+int value = {attempt};" in patch_file.read_text()

        if attempt == 1:
            (local_clone / "builder-live.log").write_text("retry build log")
            with notes.open("a") as stream:
                stream.write("\nFirst repair summary\n")
        result = BackportOutputSchema(
            success=attempt == 2,
            status=f"Repair {attempt}",
            srpm_path=local_clone / "expat.src.rpm",
            error="second build failure" if attempt == 1 else None,
        )
        return SimpleNamespace(last_message=SimpleNamespace(text=result.model_dump_json()))

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    async def create_repair_agent(*args, **kwargs):
        return flexmock(run=repair)

    async def failed_build(**kwargs):
        return BuildOutputSchema(success=False, error="initial build failure")

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway).once()
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None).once()
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None).once()
    flexmock(backport_agent).should_receive("get_agent_execution_config").and_return({}).twice()
    flexmock(backport_agent).should_receive("create_backport_agent").replace_with(create_repair_agent).twice()
    flexmock(backport_agent).should_receive("run_build").replace_with(failed_build).once()
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    run_workflow = Workflow.run

    def start_at_build(workflow, state, options=None):
        state.local_clone = local_clone
        state.unpacked_sources = upstream
        state.used_cherry_pick_workflow = True
        state.backport_result = BackportOutputSchema(
            success=True,
            status="Backported",
            srpm_path=local_clone / "expat.src.rpm",
            error=None,
        )
        workflow.set_start("run_build_agent")
        # Exercise the real build-failure routing and both repair attempts,
        # stopping before release bookkeeping or external writes.
        workflow.steps["update_release"].handler = lambda _: Workflow.END
        workflow.steps["comment_in_jira"].handler = lambda _: Workflow.END
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_build)
    state = await backport_agent.run_workflow(
        package="expat",
        dist_git_branch=branch,
        upstream_patches=[],
        jira_issue="RHEL-123",
        cve_id=None,
        dry_run=True,
        backport_agent_factory=lambda *_: None,
    )

    assert state.backport_result.success, state.backport_result.error
    assert len(attempts) == 2
    git(local_clone, "add", "-A")
    assert git(local_clone, "diff", "--cached", "--name-only") == "fix.patch"
    assert not (upstream / "build-logs").exists()
