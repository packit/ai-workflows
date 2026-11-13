import json
import os
import time
from pathlib import Path

import pytest

from ymir.agents.amalgamator import adapter


def _case(**overrides) -> dict:
    kwargs = {
        "issue_id": "RHEL-123456",
        "description": "CVE-2026-1234 not fixed in curl",
        "upstream_clone": "/work/curl-rhel-9.6-upstream",
        "workdir": "/work/ymir-runs/RHEL-123456",
        "base_commit": "9f2c1ab",
        "fix_commit": "abc1234",
        "source_branch": "origin/master",
    }
    kwargs.update(overrides)
    return adapter.build_case_config(**kwargs)


def test_commit_mode_sets_fix_commit_and_source_branch():
    config = _case()

    assert config["fix"] == {"commit": "abc1234"}
    assert config["branches"]["target"] == "9f2c1ab"
    assert config["branches"]["source"] == "origin/master"
    # The target is a SHA, never a dist-git branch name.
    assert config["branches"]["target"] != "rhel-9.6"


def test_patch_mode_keeps_the_caller_source_branch():
    config = _case(fix_commit=None, fix_patch="RHEL-123456-0.patch")

    # Amalgamator resolves a relative patch against the case file's directory,
    # so the adapter pins it to the caller's cwd.
    assert config["fix"] == {"patch": str(Path("RHEL-123456-0.patch").resolve())}
    # Amalgamator would otherwise default to origin/main, which does not exist
    # in repositories still on master.
    assert config["branches"]["source"] == "origin/master"


def test_patch_mode_omits_an_unknown_source_branch():
    config = _case(fix_commit=None, fix_patch="/abs/fix.patch", source_branch=None)

    assert "source" not in config["branches"]
    assert config["fix"] == {"patch": "/abs/fix.patch"}


def test_commit_mode_requires_a_source_branch():
    # Amalgamator rejects commit mode without branches.source; name the missing
    # argument here instead of surfacing its config error later.
    with pytest.raises(ValueError, match="source_branch"):
        _case(source_branch=None)


def test_exactly_one_fix_input_is_required():
    with pytest.raises(ValueError):
        _case(fix_commit=None, fix_patch=None)
    with pytest.raises(ValueError):
        _case(fix_commit="abc1234", fix_patch="RHEL-123456-0.patch")


def test_build_commands_default_to_empty():
    config = _case()

    assert config["build"] == {"command": "", "test_command": ""}
    assert config["repository"]["source_path"].endswith("curl-rhel-9.6-upstream")


def test_model_is_only_included_when_given():
    assert "model" not in _case()
    assert _case(model="m1")["model"] == "m1"


def test_write_case_round_trips_as_json(tmp_path):
    config = _case()
    case_path = adapter.write_case(config, tmp_path / "case.json")

    assert json.loads(case_path.read_text()) == config


def test_find_run_dir_prefers_the_newest_run(tmp_path):
    runs = tmp_path / "runs"
    (runs / "case_20260101_000000").mkdir(parents=True)
    newer = runs / "case_20260102_000000"
    newer.mkdir()
    (newer / "case.json_placeholder").write_text("x")

    assert adapter.find_run_dir(tmp_path) == newer
    assert adapter.find_run_dir(tmp_path / "missing") is None


def test_find_run_dir_ignores_runs_that_existed_before(tmp_path):
    runs = tmp_path / "runs"
    old = runs / "case_20260101_000000"
    old.mkdir(parents=True)

    # A rerun that never created its own directory has no run to report.
    assert adapter.find_run_dir(tmp_path, previous={old}) is None

    fresh = runs / "case_20260102_000000"
    fresh.mkdir()
    assert adapter.find_run_dir(tmp_path, previous={old}) == fresh


def _age(path: Path, seconds: float = 3600) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_find_run_dir_ignores_a_reused_directory_holding_old_artifacts(tmp_path):
    runs = tmp_path / "runs"
    reused = runs / "case_20260101_000000"
    reused.mkdir(parents=True)
    _age(reused)

    # Directory names have one-second resolution, so a rerun can land in a
    # directory that already existed; only content written after launch counts.
    assert adapter.find_run_dir(tmp_path, previous={reused}, not_before=time.time()) is None


def test_read_summary_ignores_a_summary_from_an_earlier_run(tmp_path):
    (tmp_path / "summary.json").write_text('{"total_targets": 1}')
    _age(tmp_path / "summary.json")

    assert adapter.read_summary(tmp_path, not_before=time.time()) is None


def test_locate_artifacts_ignores_a_patch_from_an_earlier_run(tmp_path):
    run_dir = tmp_path / "runs" / "case_1"
    target_dir = run_dir / "9f2c1ab"
    target_dir.mkdir(parents=True)
    patch = target_dir / "candidate.patch"
    patch.write_text("diff --git a/x b/x\n")
    _age(patch)

    assert adapter.locate_artifacts(run_dir, {"targets": []}, not_before=time.time()) == (None, None)


def test_locate_artifacts_uses_summary_tracker_path(tmp_path):
    run_dir = tmp_path / "runs" / "case_1"
    target_dir = run_dir / "9f2c1ab"
    target_dir.mkdir(parents=True)
    (target_dir / "candidate.patch").write_text("diff --git a/x b/x\n")
    (target_dir / "dossier.md").write_text("# Dossier\n")
    summary = {"targets": [{"tracker_path": "9f2c1ab/tracker.json"}]}

    patch, dossier = adapter.locate_artifacts(run_dir, summary)

    assert patch == target_dir / "candidate.patch"
    assert dossier == target_dir / "dossier.md"


def test_locate_artifacts_falls_back_when_summary_names_no_patch(tmp_path):
    run_dir = tmp_path / "runs" / "case_1"
    stale = run_dir / "old_target"
    stale.mkdir(parents=True)
    actual = run_dir / "9f2c1ab"
    actual.mkdir()
    (actual / "candidate.patch").write_text("diff --git a/x b/x\n")
    summary = {"targets": [{"tracker_path": "old_target/tracker.json"}]}

    patch, dossier = adapter.locate_artifacts(run_dir, summary)

    assert patch == actual / "candidate.patch"
    assert dossier is None


def test_locate_artifacts_returns_none_when_no_patch(tmp_path):
    run_dir = tmp_path / "runs" / "case_1" / "9f2c1ab"
    run_dir.mkdir(parents=True)
    (run_dir / "run_log.txt").write_text("failed")

    patch, dossier = adapter.locate_artifacts(run_dir.parent, {"targets": []})

    assert patch is None
    assert dossier is None


def test_read_summary_tolerates_missing_or_broken_files(tmp_path):
    assert adapter.read_summary(tmp_path) is None

    (tmp_path / "summary.json").write_text("{not json")
    assert adapter.read_summary(tmp_path) is None

    (tmp_path / "summary.json").write_text('{"total_targets": 1}')
    assert adapter.read_summary(tmp_path) == {"total_targets": 1}


def test_result_serializes_paths(tmp_path):
    result = adapter.AmalgamatorResult(
        status="success",
        exit_code=0,
        duration_s=12.5,
        case_path=tmp_path / "case.json",
        candidate_patch=tmp_path / "candidate.patch",
    )

    data = result.to_dict()

    assert data["case_path"] == str(tmp_path / "case.json")
    assert data["candidate_patch"] == str(tmp_path / "candidate.patch")
    assert result.produced_patch


def _fake_run(returncode: int, outcome: str | None = None):
    """Stand in for Amalgamator: write a run directory like a real invocation."""

    def fake(**kwargs):
        run_root = kwargs["case_path"].parent / "runs" / "case_20260101_000000"
        target_dir = run_root / "9f2c1ab"
        target_dir.mkdir(parents=True)
        (target_dir / "candidate.patch").write_text("diff --git a/x b/x\n")
        if outcome is not None:
            (run_root / "summary.json").write_text(
                json.dumps({"targets": [{"outcome": outcome, "tracker_path": "9f2c1ab/tracker.json"}]})
            )
        return returncode, "", "", 0.1

    return fake


def _run_case(tmp_path, **overrides):
    kwargs = {
        "amalgamator_root": tmp_path / "amalgamator",
        "issue_id": "RHEL-123456",
        "description": "CVE-2026-1234 not fixed in curl",
        "upstream_clone": tmp_path / "upstream",
        "workdir": tmp_path / "work",
        "base_commit": "9f2c1ab",
        "fix_patch": "/abs/fix.patch",
    }
    kwargs.update(overrides)
    return adapter.run_case(**kwargs)


def test_run_case_reports_success_only_for_a_clean_run(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter, "run_amalgamator", _fake_run(returncode=0, outcome="success"))

    result = _run_case(tmp_path)

    assert result.status == "success"
    assert result.outcome == "success"
    assert result.produced_patch


def test_run_case_does_not_treat_a_failed_run_patch_as_success(monkeypatch, tmp_path):
    # Amalgamator also writes candidate.patch on pipeline stop and escalation,
    # so the file alone must not be read as success.
    monkeypatch.setattr(adapter, "run_amalgamator", _fake_run(returncode=1, outcome="pipeline_stop"))

    result = _run_case(tmp_path)

    assert result.status == "failed"
    assert result.outcome == "pipeline_stop"
    assert result.produced_patch


def test_run_case_reports_an_error_when_amalgamator_never_started(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter, "run_amalgamator", lambda **kwargs: (2, "", "bad config", 0.1))

    result = _run_case(tmp_path)

    assert result.status == "error"
    assert result.run_dir is None
    assert result.stderr_tail == ["bad config"]


def test_run_case_reports_an_error_for_a_wrong_amalgamator_root(tmp_path):
    # An unmounted checkout must not be reported as a failed backport attempt.
    result = _run_case(tmp_path, amalgamator_root=tmp_path / "not-amalgamator")

    assert result.status == "error"
    assert result.exit_code == -2
    assert "not an Amalgamator checkout" in result.stderr_tail[-1]


def test_run_case_resolves_the_models_path_against_the_callers_cwd(monkeypatch, tmp_path):
    seen = {}

    def fake(**kwargs):
        seen["models_path"] = kwargs["models_path"]
        return 2, "", "", 0.1

    monkeypatch.setattr(adapter, "run_amalgamator", fake)

    _run_case(tmp_path, models_path="conf/models.yaml")

    # Amalgamator runs with cwd set to the case directory, so a relative path
    # must be resolved before it is passed on.
    assert seen["models_path"] == str(Path("conf/models.yaml").resolve())
