import pytest

from ymir.agents.backport_agent import (
    BackportRetryMode,
    BackportState,
    _build_inherited_publication_checkpoint,
    _can_attempt_ystream_inheritance,
    _configure_task_retry,
    _disable_ystream_inheritance,
    _get_shipped_zstream_candidates,
    _inherit_prep_error,
    _move_build_logs,
    _remote_branch_matches_commit,
    _restore_inherited_publication,
    _schedule_inherit_cleanup_retry,
    _update_fix_attempts_log,
    _validate_inherited_staged_files,
)
from ymir.agents.ystream_inherit import (
    BrewSource,
    InheritCandidateError,
    InheritedPatchApplyError,
    IntegratedChange,
)
from ymir.common.models import (
    BackportOutputSchema,
    LogOutputSchema,
    ShippedZStreamCandidate,
    Task,
)


def test_get_shipped_zstream_candidates_from_triage_state():
    triage_state = {
        "cve_eligibility_result": {
            "is_cve": True,
            "eligibility": "immediately",
            "reason": "clone shipped",
            "shipped_zstream_candidates": [
                {
                    "issue_key": "RHEL-123",
                    "fixed_in_build": "curl-8.0.1-2.el9_7",
                    "fix_versions": ["rhel-9.7.z"],
                }
            ],
        }
    }

    assert _get_shipped_zstream_candidates(triage_state) == [
        ShippedZStreamCandidate(
            issue_key="RHEL-123",
            fixed_in_build="curl-8.0.1-2.el9_7",
            fix_versions=["rhel-9.7.z"],
        )
    ]


def test_get_shipped_zstream_candidates_supports_old_payloads():
    assert _get_shipped_zstream_candidates({}) == []
    assert _get_shipped_zstream_candidates({"cve_eligibility_result": None}) == []


def _state(**updates):
    data = {
        "jira_issue": "RHEL-999",
        "package": "curl",
        "dist_git_branch": "c9s",
        "upstream_patches": ["https://example.com/fix.patch"],
        "cve_id": "CVE-2026-1234",
        "fix_version": "rhel-9.8",
        "shipped_zstream_candidates": [
            ShippedZStreamCandidate(
                issue_key="RHEL-123",
                fixed_in_build="curl-8.0.1-2.el9_7",
                fix_versions=["rhel-9.7.z"],
            )
        ],
    }
    data.update(updates)
    return BackportState(**data)


def test_ystream_inheritance_requires_y_fix_version_and_cs_target():
    assert _can_attempt_ystream_inheritance(_state())
    assert not _can_attempt_ystream_inheritance(_state(fix_version="rhel-9.7.z"))
    assert not _can_attempt_ystream_inheritance(_state(dist_git_branch="rhel-9.8"))
    assert not _can_attempt_ystream_inheritance(_state(shipped_zstream_candidates=[]))
    assert not _can_attempt_ystream_inheritance(_state(inheritance_disabled=True))


def test_disabling_inheritance_is_durable_in_task_metadata():
    state = _state()
    metadata = {}

    _disable_ystream_inheritance(state, metadata)

    assert state.inheritance_disabled
    assert metadata["ystream_inheritance_disabled"] is True


def test_patch_prep_failure_requires_immediate_normal_backport():
    change = IntegratedChange(
        commit_sha="a" * 40,
        commit_message="Fix",
        changed_files=["curl.spec", "fix.patch"],
        patch_files=["fix.patch"],
    )

    assert isinstance(_inherit_prep_error("prep failed: hunk rejected", change), InheritedPatchApplyError)
    assert isinstance(_inherit_prep_error("patch applied with fuzz", change), InheritedPatchApplyError)
    assert _inherit_prep_error("prep completed successfully", change) is None


def test_spec_only_prep_failure_remains_candidate_failure():
    change = IntegratedChange(
        commit_sha="a" * 40,
        commit_message="Fix",
        changed_files=["curl.spec"],
    )

    error = _inherit_prep_error("prep failed", change)
    assert isinstance(error, InheritCandidateError)
    assert not isinstance(error, InheritedPatchApplyError)


def test_inherited_staging_rejects_missing_or_unexpected_files():
    _validate_inherited_staged_files("curl.spec\nfix.patch\n", ["curl.spec", "fix.patch"])

    with pytest.raises(InheritCandidateError, match=r"extra\.patch"):
        _validate_inherited_staged_files(
            "curl.spec\nfix.patch\nextra.patch\n",
            ["curl.spec", "fix.patch"],
        )


def test_cleanup_reclone_retries_single_inherit_source_once():
    state = _state()

    assert _schedule_inherit_cleanup_retry(state)
    assert state.inherit_cleanup_retried

    assert not _schedule_inherit_cleanup_retry(state)


def test_remote_branch_must_point_at_exact_inherited_commit():
    commit_sha = "a" * 40

    assert _remote_branch_matches_commit(commit_sha.upper(), commit_sha)
    assert not _remote_branch_matches_commit("b" * 40, commit_sha)
    assert not _remote_branch_matches_commit(None, commit_sha)


def _published_state(**updates):
    state = _state(
        fork_url="https://gitlab.com/ymir/curl",
        update_branch="automated-package-update-RHEL-999",
        inherit_local_commit="a" * 40,
        inherit_candidate=_state().shipped_zstream_candidates[0],
        inherit_source=BrewSource(
            nvr="curl-8.0.1-2.el9_7",
            repository_url="https://gitlab.com/redhat/rhel/rpms/curl",
            commit_sha="b" * 40,
            epoch=0,
            version="8.0.1",
        ),
        inherit_change=IntegratedChange(
            commit_sha="c" * 40,
            commit_message="Fix CVE\n\nResolves: RHEL-123",
            changed_files=["curl.spec", "fix.patch"],
        ),
        inherit_mr_description="Inherited fix",
        log_result=LogOutputSchema(title="Fix CVE", description="Inherited fix"),
        backport_result=BackportOutputSchema(
            success=True,
            status="Inherited from RHEL-123",
            srpm_path=None,
            error=None,
        ),
    )
    return state.model_copy(update=updates)


def test_resume_retry_persists_inherited_publication_checkpoint():
    state = _published_state(retry_mode=BackportRetryMode.RESUME_INHERITED_MR)
    state.inherited_publication_checkpoint = _build_inherited_publication_checkpoint(state)
    task = Task(metadata={})

    assert _configure_task_retry(task, state)
    checkpoint = task.metadata["inherited_publication_checkpoint"]
    assert checkpoint["local_commit"] == "a" * 40
    assert checkpoint["source_issue_key"] == "RHEL-123"


def test_restore_checkpoint_resumes_only_publication_state():
    published = _published_state()
    checkpoint = _build_inherited_publication_checkpoint(published)
    state = _state(inherited_publication_checkpoint=checkpoint)

    assert _restore_inherited_publication(state) == checkpoint
    assert state.retry_mode == BackportRetryMode.RESUME_INHERITED_MR
    assert state.fork_url == published.fork_url
    assert state.update_branch == published.update_branch
    assert state.inherit_local_commit == published.inherit_local_commit
    assert state.backport_result.success
    assert state.local_clone is None


def test_invariant_failure_disables_queue_retry():
    state = _state(retry_mode=BackportRetryMode.NONE)

    assert not _configure_task_retry(Task(metadata={}), state)


class TestMoveBuildLogs:
    def test_moves_log_files(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "build.log").write_text("log content")
        (source / "root.log").write_text("root content")

        target = tmp_path / "target"
        _move_build_logs(source, target)

        assert (target / "build.log").exists()
        assert (target / "root.log").exists()
        assert not (source / "build.log").exists()
        assert not (source / "root.log").exists()

    def test_moves_gz_files(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "build.log.gz").write_bytes(b"\x1f\x8b fake gz")

        target = tmp_path / "target"
        _move_build_logs(source, target)

        assert (target / "build.log.gz").exists()
        assert not (source / "build.log.gz").exists()

    def test_ignores_non_log_files(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "package.spec").write_text("spec content")
        (source / "README.md").write_text("readme")
        (source / "build.log").write_text("log")

        target = tmp_path / "target"
        _move_build_logs(source, target)

        assert (target / "build.log").exists()
        assert not (target / "package.spec").exists()
        assert not (target / "README.md").exists()
        assert (source / "package.spec").exists()
        assert (source / "README.md").exists()

    def test_creates_target_directory(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "build.log").write_text("log")

        target = tmp_path / "nested" / "deep" / "target"
        _move_build_logs(source, target)

        assert target.exists()
        assert (target / "build.log").exists()

    def test_noop_when_no_logs(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "README.md").write_text("no logs here")

        target = tmp_path / "target"
        _move_build_logs(source, target)

        assert target.exists()
        assert list(target.iterdir()) == []


class TestUpdateFixAttemptsLog:
    def test_creates_log_on_first_attempt(self, tmp_path):
        _update_fix_attempts_log(tmp_path, 1, "undefined reference to 'foo'")

        log = tmp_path / "fix-attempts.md"
        assert log.exists()
        content = log.read_text()
        assert "# Fix Attempts Log" in content
        assert "## Initial build failure" in content
        assert "## Attempt 1" in content
        assert "undefined reference to 'foo'" in content

    def test_appends_on_subsequent_attempt(self, tmp_path):
        _update_fix_attempts_log(tmp_path, 1, "first error")
        _update_fix_attempts_log(tmp_path, 2, "second error")

        content = (tmp_path / "fix-attempts.md").read_text()
        assert "## Attempt 1" in content
        assert "## Attempt 2" in content
        assert "first error" in content
        assert "second error" in content

    def test_preserves_existing_content_on_append(self, tmp_path):
        _update_fix_attempts_log(tmp_path, 1, "original error")
        original_content = (tmp_path / "fix-attempts.md").read_text()

        _update_fix_attempts_log(tmp_path, 2, "new error")
        new_content = (tmp_path / "fix-attempts.md").read_text()

        assert new_content.startswith(original_content.rstrip())

    def test_error_wrapped_in_code_block(self, tmp_path):
        _update_fix_attempts_log(tmp_path, 1, "make[2]: *** [Makefile:42] Error 1")

        content = (tmp_path / "fix-attempts.md").read_text()
        assert "```\nmake[2]: *** [Makefile:42] Error 1\n```" in content
