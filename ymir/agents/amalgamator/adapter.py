"""Adapter from Ymir state to Amalgamator's case configuration.

Amalgamator takes one case file (YAML or JSON) describing a single backport:
an upstream clone, a base commit to port onto, the upstream fix, and an
optional description plus build/test commands. It writes its results under
``runs/<case>_<timestamp>/<target>/`` relative to its working directory.

This module is deliberately standalone and stdlib-only: it converts plain
values into a case file, runs ``python -m auto_bug_fix.run_bug_fix`` as a
subprocess, and reads the artifacts back. It knows nothing about Ymir's
workflow, so it can be exercised on its own.

Usage::

    python -m ymir.agents.amalgamator.adapter \\
        --amalgamator-root /path/to/amalgamator \\
        --jira-issue RHEL-123456 \\
        --description "CVE-2026-1234 not fixed in curl" \\
        --upstream-clone /work/curl-rhel-9.6/curl-rhel-9.6-upstream \\
        --fix-patch /work/curl-rhel-9.6/RHEL-123456-0.patch \\
        --base-commit 9f2c1ab \\
        --workdir /work/ymir-runs/RHEL-123456

Exit codes:
    0  Amalgamator succeeded (patch produced, exit 0, target outcome success)
    1  Amalgamator ran but did not succeed, with or without a patch
    2  the adapter itself failed (bad input, timeout, missing output)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Runtime is model-dependent: GLM-5.3 took between 2h20m and 4h07m per case on
# the e2e fixtures, and a faster model finishes sooner. The default is sized so
# a slow model still completes instead of being killed and recorded as a
# failure; a shorter value is how to cap an experiment.
DEFAULT_TIMEOUT_SECONDS = 21600


@dataclass
class AmalgamatorResult:
    """Outcome of one Amalgamator run, flattened for comparison and logging."""

    status: str  # "success", "failed" (ran, no patch) or "error" (never completed)
    exit_code: int
    duration_s: float
    case_path: Path
    run_dir: Path | None = None
    candidate_patch: Path | None = None
    dossier: Path | None = None
    outcome: str | None = None
    amalgamator_duration_s: float | None = None
    cost_usd: float | None = None
    target_branch: str | None = None
    error: str | None = None
    stderr_tail: list[str] = field(default_factory=list)

    @property
    def produced_patch(self) -> bool:
        return self.candidate_patch is not None

    def to_dict(self) -> dict:
        data = asdict(self)
        return {key: str(value) if isinstance(value, Path) else value for key, value in data.items()}


def build_case_config(
    *,
    issue_id: str,
    description: str,
    upstream_clone: Path | str,
    workdir: Path | str,
    base_commit: str,
    fix_patch: str | None = None,
    fix_commit: str | None = None,
    source_branch: str | None = None,
    build_command: str = "",
    test_command: str = "",
    model: str | None = None,
) -> dict:
    """Build Amalgamator's case mapping from Ymir values.

    ``fix_patch`` and ``fix_commit`` are mutually exclusive. A patch path is
    resolved against the caller's current directory and stored absolute, because
    Amalgamator resolves relative paths against the case file's directory.
    Commit mode uses a commit that already exists in ``upstream_clone`` and
    requires ``source_branch``.
    """
    if bool(fix_patch) == bool(fix_commit):
        raise ValueError("exactly one of fix_patch or fix_commit is required")

    config: dict = {
        "issue_id": issue_id,
        "bug_description": description,
        "repository": {
            "source_path": str(Path(upstream_clone).resolve()),
            "workdir": str(Path(workdir).resolve()),
        },
        # ``target`` is the OLD commit to port onto — a SHA, not a branch name.
        "branches": {"target": base_commit},
        "build": {"command": build_command, "test_command": test_command},
    }
    if fix_commit:
        if not source_branch:
            # Amalgamator requires branches.source in commit mode; failing here
            # names the missing argument instead of surfacing its config error.
            raise ValueError("source_branch is required with fix_commit")
        config["fix"] = {"commit": fix_commit}
    else:
        # Amalgamator resolves a relative patch path against the case file's
        # directory, which is not where a CLI caller's cwd is. Resolve it now,
        # while the caller's cwd is still in effect.
        patch_path = Path(fix_patch).expanduser()
        if not patch_path.is_absolute():
            patch_path = (Path.cwd() / patch_path).resolve()
        config["fix"] = {"patch": str(patch_path)}
    # Amalgamator defaults this to origin/main in patch mode, which does not
    # exist in repositories still on master. Always name the real ref.
    if source_branch:
        config["branches"]["source"] = source_branch
    if model:
        config["model"] = model
    return config


def write_case(config: dict, case_path: Path) -> Path:
    """Write the case file. JSON is accepted by Amalgamator, so no YAML dependency."""
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return case_path


def run_amalgamator(
    *,
    amalgamator_root: Path | str,
    case_path: Path,
    models_path: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
) -> tuple[int, str, str, float]:
    """Run Amalgamator on ``case_path``; return (returncode, stdout, stderr, elapsed)."""
    root = Path(amalgamator_root).resolve()
    if not (root / "auto_bug_fix" / "run_bug_fix.py").is_file():
        # A wrong or unmounted checkout would otherwise look like a normal
        # run failure (Python exits 1 on ImportError).
        return -2, "", f"adapter: {root} is not an Amalgamator checkout", 0.0
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(root), env.get("PYTHONPATH", "")) if part)
    interpreter = python or os.getenv("AMALGAMATOR_PYTHON") or sys.executable
    cmd = [interpreter, "-m", "auto_bug_fix.run_bug_fix", case_path.name]
    if models_path:
        cmd.extend(["--models", models_path])

    started = time.time()
    try:
        # Fixed argv, no shell: the command is built above, not passed in.
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=str(case_path.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - started
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return -1, stdout, stderr + f"\nadapter: killed after {timeout}s", elapsed

    return proc.returncode, proc.stdout or "", proc.stderr or "", time.time() - started


# Run directory names have one-second resolution and are created with
# ``exist_ok``, so a second run started in the same second lands in the same
# directory. Artifacts older than this slack are not from the current run.
_FRESHNESS_SLACK_S = 2.0


def _is_fresh(path: Path, not_before: float | None) -> bool:
    if not_before is None:
        return True
    try:
        return path.stat().st_mtime >= not_before - _FRESHNESS_SLACK_S
    except OSError:
        return False


def find_run_dir(
    workdir: Path, previous: set[Path] | None = None, not_before: float | None = None
) -> Path | None:
    """Return the run directory this invocation created, if any.

    ``runs/`` lives in the working directory and accumulates across runs, and a
    start that fails on configuration exits before creating a directory. When
    *previous* lists the directories that existed beforehand, a rerun that
    failed must not be reported with the previous run's artifacts, so a
    directory counts only when it is new or was written after *not_before*.
    """
    runs_root = workdir / "runs"
    if not runs_root.is_dir():
        return None
    candidates = [path for path in runs_root.iterdir() if path.is_dir()]
    if previous is not None:
        candidates = [
            path
            for path in candidates
            if path not in previous or (not_before is not None and _is_fresh(path, not_before))
        ]
    elif not_before is not None:
        candidates = [path for path in candidates if _is_fresh(path, not_before)]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def read_summary(run_dir: Path, not_before: float | None = None) -> dict | None:
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file() or not _is_fresh(summary_path, not_before):
        return None
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def locate_artifacts(
    run_dir: Path, summary: dict | None, not_before: float | None = None
) -> tuple[Path | None, Path | None]:
    """Return ``(candidate.patch, dossier.md)``, preferring paths recorded in summary.json."""
    target_dirs: list[Path] = []
    for target in (summary or {}).get("targets", []):
        tracker = target.get("tracker_path")
        if tracker:
            target_dirs.append(run_dir / Path(tracker).parent)
    # summary.json can be unreadable or name a directory that holds no patch;
    # the per-target directories are the source of truth either way.
    target_dirs.extend(path for path in run_dir.iterdir() if path.is_dir() and path not in target_dirs)

    for target_dir in target_dirs:
        patch = target_dir / "candidate.patch"
        if patch.is_file() and _is_fresh(patch, not_before):
            dossier = target_dir / "dossier.md"
            return patch, dossier if dossier.is_file() and _is_fresh(dossier, not_before) else None
    return None, None


def run_case(
    *,
    amalgamator_root: Path | str,
    issue_id: str,
    description: str,
    upstream_clone: Path | str,
    workdir: Path | str,
    base_commit: str,
    fix_patch: str | None = None,
    fix_commit: str | None = None,
    source_branch: str | None = None,
    build_command: str = "",
    test_command: str = "",
    model: str | None = None,
    models_path: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
) -> AmalgamatorResult:
    """Write the case file, run Amalgamator, and collect the result."""
    workdir_path = Path(workdir).resolve()
    runs_root = workdir_path / "runs"
    existing_runs = set(runs_root.iterdir()) if runs_root.is_dir() else set()
    # Amalgamator runs with cwd set to the case directory, so a relative models
    # path would be resolved there instead of against the caller's cwd.
    resolved_models = str(Path(models_path).expanduser().resolve()) if models_path else None
    case_path = workdir_path / "case.json"
    config = build_case_config(
        issue_id=issue_id,
        description=description,
        upstream_clone=upstream_clone,
        workdir=workdir_path,
        base_commit=base_commit,
        fix_patch=fix_patch,
        fix_commit=fix_commit,
        source_branch=source_branch,
        build_command=build_command,
        test_command=test_command,
        model=model,
    )
    write_case(config, case_path)

    launched = time.time()
    returncode, _stdout, stderr, elapsed = run_amalgamator(
        amalgamator_root=amalgamator_root,
        case_path=case_path,
        models_path=resolved_models,
        timeout=timeout,
        python=python,
    )

    run_dir = find_run_dir(workdir_path, previous=existing_runs, not_before=launched)
    summary = read_summary(run_dir, not_before=launched) if run_dir else None
    patch, dossier = locate_artifacts(run_dir, summary, not_before=launched) if run_dir else (None, None)
    targets = (summary or {}).get("targets") or [{}]
    target_info = targets[0] or {}
    outcome = target_info.get("outcome")

    # Amalgamator writes candidate.patch on PipelineStop, PipelineEscalation and
    # partial_success as well, so a patch on disk is not by itself success. The
    # exit code and the target outcome are what distinguish a clean run.
    if patch and returncode == 0 and outcome in (None, "success"):
        status = "success"
    elif summary is not None or returncode in (0, 1):
        status = "failed"
    else:
        status = "error"

    return AmalgamatorResult(
        status=status,
        exit_code=returncode,
        duration_s=round(elapsed, 1),
        case_path=case_path,
        run_dir=run_dir,
        candidate_patch=patch,
        dossier=dossier,
        outcome=outcome,
        amalgamator_duration_s=target_info.get("duration_s"),
        cost_usd=target_info.get("cost_usd"),
        target_branch=target_info.get("target_branch"),
        error=target_info.get("error"),
        stderr_tail=stderr.strip().splitlines()[-20:],
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Amalgamator on inputs Ymir already has.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--amalgamator-root", required=True, help="Amalgamator checkout")
    parser.add_argument("--jira-issue", required=True, help="Label only; used in artifact names")
    parser.add_argument("--description", required=True, help="Short bug description")
    parser.add_argument("--upstream-clone", required=True, help="Local clone of the upstream project")
    parser.add_argument("--base-commit", required=True, help="Old commit to port onto (a SHA)")
    fix_group = parser.add_mutually_exclusive_group(required=True)
    fix_group.add_argument("--fix-commit", help="Upstream commit containing the fix")
    fix_group.add_argument("--fix-patch", help="Upstream fix patch, relative to the current directory")
    parser.add_argument("--workdir", required=True, help="Per-run scratch directory")
    parser.add_argument("--source-branch", default=None, help="Branch containing the fix commit")
    parser.add_argument("--model", default=None, help="Amalgamator model profile or model name")
    parser.add_argument("--models", default=None, help="Path to Amalgamator's models.yaml")
    parser.add_argument("--build-command", default="", help="Optional build command")
    parser.add_argument("--test-command", default="", help="Optional test command")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Seconds before the run is killed"
    )
    parser.add_argument("--output", default=None, help="Write the result JSON here as well")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_case(
        amalgamator_root=args.amalgamator_root,
        issue_id=args.jira_issue,
        description=args.description,
        upstream_clone=args.upstream_clone,
        workdir=args.workdir,
        base_commit=args.base_commit,
        fix_patch=args.fix_patch,
        fix_commit=args.fix_commit,
        source_branch=args.source_branch,
        build_command=args.build_command,
        test_command=args.test_command,
        model=args.model,
        models_path=args.models,
        timeout=args.timeout,
    )

    rendered = json.dumps(result.to_dict(), indent=2)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")

    if result.status == "success":
        return 0
    return 1 if result.status == "failed" else 2


if __name__ == "__main__":
    sys.exit(main())
