"""Build-repair diagnostics must not become part of generated source patches."""

import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock

from ymir.agents import backport_agent
from ymir.agents import tasks as agent_tasks
from ymir.common.models import BackportOutputSchema, BuildOutputSchema, LogOutputSchema
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
        # The fix agent never builds; it only produces a corrected backport. Each
        # invocation follows a failed build from the dedicated build step.
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
        # Each repair produces a candidate fix; the build step decides success.
        result = BackportOutputSchema(
            success=True,
            status=f"Repair {attempt}",
            srpm_path=local_clone / "expat.src.rpm",
            error=None,
        )
        return SimpleNamespace(last_message=SimpleNamespace(text=result.model_dump_json()))

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    async def create_repair_agent(*args, **kwargs):
        return flexmock(run=repair)

    # The build step is the only builder: fail twice (driving two repair cycles),
    # then pass so the workflow proceeds to release bookkeeping.
    builds = []

    async def staged_build(**kwargs):
        builds.append(kwargs)
        if len(builds) == 1:
            return BuildOutputSchema(success=False, error="initial build failure")
        if len(builds) == 2:
            return BuildOutputSchema(success=False, error="second build failure")
        return BuildOutputSchema(success=True, error=None)

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway).once()
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None).once()
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None).once()
    flexmock(backport_agent).should_receive("get_agent_execution_config").and_return({}).twice()
    flexmock(backport_agent).should_receive("create_backport_agent").replace_with(create_repair_agent).twice()
    flexmock(backport_agent).should_receive("run_build").replace_with(staged_build)

    # The single build step commits + pushes before building; keep those out of
    # the real checkout so the Git assertions below see only the repair patch.
    async def _commit(_clone, _message, *a, **k):
        return "a" * 40

    async def _push(*a, **k):
        return None

    async def _no_zstream_label(*a, **k):
        return False

    flexmock(agent_tasks).should_receive("commit_changes").replace_with(_commit)
    flexmock(agent_tasks).should_receive("push_changes").replace_with(_push)
    flexmock(agent_tasks).should_receive("needs_zstream_target_label").replace_with(_no_zstream_label)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    run_workflow = Workflow.run

    def start_at_build(workflow, state, options=None):
        state.local_clone = local_clone
        state.unpacked_sources = upstream
        state.used_cherry_pick_workflow = True
        state.fork_url = "https://fork.example/repo.git"
        state.update_branch = "ymir-RHEL-123"
        state.backport_log = ["Backported the fix"]
        state.log_result = LogOutputSchema(title="Fix the thing", description="Backport of the fix")
        state.backport_result = BackportOutputSchema(
            success=True,
            status="Backported",
            srpm_path=local_clone / "expat.src.rpm",
            error=None,
        )
        # The build step is the sole builder for both backends; a failed build
        # routes to fix_build_error and loops back through update_release. Short
        # update_release straight back to the build so the loop stays focused on
        # the repair/archiving behaviour under test.
        workflow.set_start("commit_push_and_build")
        workflow.steps["update_release"].handler = lambda _: "commit_push_and_build"
        workflow.steps["submit_consolidation_job"].handler = lambda _: Workflow.END
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
    assert len(builds) == 3, "the build step is the sole builder and runs each cycle"
    git(local_clone, "add", "-A")
    assert git(local_clone, "diff", "--cached", "--name-only") == "fix.patch"
    assert not (upstream / "build-logs").exists()


@pytest.mark.asyncio
async def test_retry_squashes_into_single_commit(monkeypatch, tmp_path):
    """Each backport yields one commit; agent build/fix attempts never reach history."""
    local_clone = tmp_path / "expat"

    def git(repo, *args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    local_clone.mkdir()
    git(local_clone, "init", "-q")
    git(local_clone, "config", "user.name", "Ymir Tests")
    git(local_clone, "config", "user.email", "ymir-tests@example.com")
    (local_clone / "expat.spec").write_text("Release: 1\n")
    git(local_clone, "add", "-A")
    git(local_clone, "commit", "-m", "Import package")
    base = git(local_clone, "rev-parse", "HEAD")

    # First backport candidate already staged on top of the pristine base.
    (local_clone / "expat.spec").write_text("Release: 2\n")
    (local_clone / "0001-fix.patch").write_text("patch v1\n")
    git(local_clone, "add", "-A")

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    # Build fails once (driving one fix cycle), then passes.
    builds = []

    async def staged_build(**kwargs):
        builds.append(kwargs)
        return BuildOutputSchema(success=len(builds) >= 2, error=None if len(builds) >= 2 else "boom")

    # The fix agent produces a fresh candidate: it rewrites the patch and stages it,
    # the same way the real fix_build_error leaves a new staged tree behind.
    fixes = []

    def fake_fix(_state):
        fixes.append(True)
        (local_clone / "expat.spec").write_text("Release: 3\n")
        (local_clone / "0001-fix.patch").write_text("patch v2\n")
        git(local_clone, "add", "-A")
        return "update_release"

    flexmock(backport_agent).should_receive("mcp_tools").replace_with(gateway)
    flexmock(backport_agent).should_receive("create_log_agent").and_return(None)
    flexmock(backport_agent).should_receive("get_mock_local_tool_env").and_return(None)
    flexmock(backport_agent).should_receive("get_agent_execution_config").and_return({})
    flexmock(backport_agent).should_receive("run_build").replace_with(staged_build)

    async def _push(*a, **k):
        return None

    async def _no_zstream_label(*a, **k):
        return False

    flexmock(agent_tasks).should_receive("push_changes").replace_with(_push)
    flexmock(agent_tasks).should_receive("needs_zstream_target_label").replace_with(_no_zstream_label)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")

    run_workflow = Workflow.run

    def start_at_build(workflow, state, options=None):
        state.local_clone = local_clone
        state.backport_base_head = base
        state.used_cherry_pick_workflow = True
        state.fork_url = "https://fork.example/repo.git"
        state.update_branch = "ymir-RHEL-123"
        state.backport_log = ["Backported the fix"]
        state.log_result = LogOutputSchema(title="Fix the thing", description="Backport of the fix")
        state.backport_result = BackportOutputSchema(
            success=True, status="Backported", srpm_path=local_clone / "expat.src.rpm", error=None
        )
        workflow.set_start("commit_push_and_build")
        workflow.steps["fix_build_error"].handler = fake_fix
        workflow.steps["update_release"].handler = lambda _: "commit_push_and_build"
        workflow.steps["submit_consolidation_job"].handler = lambda _: Workflow.END
        workflow.steps["comment_in_jira"].handler = lambda _: Workflow.END
        return run_workflow(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_build)
    state = await backport_agent.run_workflow(
        package="expat",
        dist_git_branch="c10s",
        upstream_patches=["0001-fix.patch"],
        jira_issue="RHEL-123",
        cve_id=None,
        dry_run=True,
        backport_agent_factory=lambda *_: None,
    )

    assert state.backport_result.success, state.backport_result.error
    assert len(fixes) == 1
    assert len(builds) == 2
    # Exactly one commit sits on top of the pristine base, and it carries the
    # final candidate - the intermediate build/fix attempt is gone from history.
    assert git(local_clone, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert git(local_clone, "rev-parse", "HEAD~1") == base
    assert git(local_clone, "show", "HEAD:0001-fix.patch") == "patch v2"
    assert git(local_clone, "show", "HEAD:expat.spec") == "Release: 3"
