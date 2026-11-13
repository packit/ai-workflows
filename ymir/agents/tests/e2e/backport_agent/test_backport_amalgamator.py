"""E2E arm: run Amalgamator on the same fixtures the backport agent uses.

This suite does not run the Ymir agent. It builds Amalgamator's inputs the way
a workflow step would — package version from the dist-git spec at the pre-fix
commit, upstream fix from the fixture's patch URL, description from the mock
Jira data — and then compares Amalgamator's ``candidate.patch`` with the
fixture's reference patch.

Required environment:

``MOCK_JIRA=true`` and ``DRY_RUN=true``
    Same safety gate as the agent suite; this one does not talk to Jira at all,
    but the container is configured the same way.

``AMALGAMATOR_ROOT``
    Path to an Amalgamator checkout (Python 3.12+ with its dependencies
    installed). The suite is skipped when unset.

Optional:

``AMALGAMATOR_MODEL``
    Profile id from a ``models.yaml`` registry, or a bare model name such as
    ``claude-sonnet-5-5``. A profile carries the whole transport; a bare name
    takes its key from ``ANTHROPIC_API_KEY``/``OPENAI_API_KEY``. Unset uses the
    registry's ``default`` profile.

``AMALGAMATOR_MODELS``
    Path to ``models.yaml`` as seen **inside** the container, for example
    ``/home/beeai/amalgamator-models/models.yaml``. Amalgamator tries this
    explicit path first, then a ``models.yaml`` beside the case file, then
    ``~/.config/amalgamator/models.yaml``; registries are not merged, and a
    missing explicit path fails rather than falling back. One model serves
    every phase.

``BACKPORT_E2E_ONLY_ISSUES``
    Comma-separated Jira keys; every other fixture is skipped. One case can
    take hours, so this is how to smoke-test the arm on a single issue.

``AMALGAMATOR_TIMEOUT``     seconds per case (default 21600, six hours, so a slow
    model still completes; GLM-5.3 took 2h20m to 4h07m per case)
``AMALGAMATOR_E2E_INFORMATIONAL=true``
    Record results without failing when Amalgamator runs but does not
    succeed. Adapter errors still fail. Use this for the first runs; set it
    to ``false`` once the numbers are stable.

Run::

    MOCK_JIRA=true DRY_RUN=true \\
    AMALGAMATOR_ROOT=/path/to/amalgamator \\
    pytest ymir/agents/tests/e2e/backport_agent/test_backport_amalgamator.py
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from ymir.agents.amalgamator import adapter
from ymir.agents.amalgamator import inputs as arm_inputs
from ymir.agents.tests.e2e.backport_agent.amalgamator_arm import (
    ArmUnavailableError,
    collect_inputs,
    run_arm,
)
from ymir.common.mock_repos import load_all_fixture_configs, setup_mock_repos

logger = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR = Path(__file__).parent.parent / "mock_repos" / "backport"
DEFAULT_ARTIFACTS_DIR = Path("/tmp/backport_e2e_artifacts/amalgamator")
SHARED_BARE_REPOS_DIR = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / "mock_bare_amalgamator"


@dataclass
class AmalgamatorCase:
    jira_issue: str
    package: str
    upstream_patch_url: str
    expected: dict
    slow: bool = False

    def __repr__(self) -> str:
        return f"AmalgamatorCase({self.jira_issue})"


def _fixtures_dir() -> Path:
    return Path(os.getenv("BACKPORT_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR))


def _load_test_cases() -> list[AmalgamatorCase]:
    cases = []
    for config in load_all_fixture_configs(_fixtures_dir()).values():
        case_input = config.get("input") or {}
        urls = case_input.get("upstream_patches") or []
        if not case_input.get("jira_issue") or not case_input.get("package") or not urls:
            continue
        cases.append(
            AmalgamatorCase(
                jira_issue=case_input["jira_issue"],
                package=case_input["package"],
                upstream_patch_url=urls[0],
                expected=config.get("expected") or {},
                slow=bool(config.get("slow")),
            )
        )
    return cases


test_cases = _load_test_cases()


def _parametrize_cases():
    excluded = {
        issue.strip() for issue in os.getenv("BACKPORT_E2E_EXCLUDE_ISSUES", "").split(",") if issue.strip()
    }
    only = {issue.strip() for issue in os.getenv("BACKPORT_E2E_ONLY_ISSUES", "").split(",") if issue.strip()}
    params = []
    for case in test_cases:
        marks = [pytest.mark.slow] if case.slow else []
        if case.jira_issue in excluded:
            marks.append(pytest.mark.skip(reason="excluded by BACKPORT_E2E_EXCLUDE_ISSUES"))
        elif only and case.jira_issue not in only:
            marks.append(pytest.mark.skip(reason="not selected by BACKPORT_E2E_ONLY_ISSUES"))
        params.append(pytest.param(case, id=case.jira_issue, marks=marks))
    return params


_amalgamator_params = _parametrize_cases()


@pytest.fixture(scope="session", autouse=True)
def safety_gate():
    mock_jira = os.getenv("MOCK_JIRA", "").lower()
    dry_run = os.getenv("DRY_RUN", "").lower()
    if mock_jira != "true" and dry_run != "true":
        pytest.fail("Refusing to run e2e without MOCK_JIRA=true or DRY_RUN=true")


def _amalgamator_root() -> Path | None:
    """Return the Amalgamator checkout, or ``None`` when it is not available.

    The suite is skipped rather than failed when a machine has no checkout:
    Ymir's own CI does not carry Amalgamator.
    """
    raw = os.getenv("AMALGAMATOR_ROOT")
    if not raw:
        return None
    root = Path(raw)
    if not (root / "auto_bug_fix" / "run_bug_fix.py").is_file():
        return None
    return root


def _amalgamator_interpreter() -> str | None:
    interpreter = os.getenv("AMALGAMATOR_PYTHON")
    if not interpreter:
        return None
    if not (Path(interpreter).is_file() and os.access(interpreter, os.X_OK)):
        return None
    return interpreter


@pytest.fixture(scope="session", autouse=True)
def mock_repos(request):
    """Bare-clone the fixture dist-git repos at their pre-fix commits.

    Kept in its own directory so this suite can run beside the agent suite
    without either deleting the other's clones.
    """
    if _amalgamator_root() is None:
        yield {}
        return

    fixtures = load_all_fixture_configs(_fixtures_dir())
    selected = {
        item.callspec.params["case"].jira_issue
        for item in request.session.items
        if hasattr(item, "callspec")
        and "case" in item.callspec.params
        and not any(item.iter_markers(name="skip"))
    }

    if SHARED_BARE_REPOS_DIR.exists():
        shutil.rmtree(SHARED_BARE_REPOS_DIR)
    SHARED_BARE_REPOS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        for issue_key in sorted(selected):
            config = fixtures.get(issue_key) or {}
            repos = config.get("repos") or []
            if not repos:
                continue
            setup_mock_repos(repos, issue_key, SHARED_BARE_REPOS_DIR)
        yield {key: fixtures[key] for key in selected if key in fixtures}
    finally:
        shutil.rmtree(SHARED_BARE_REPOS_DIR, ignore_errors=True)


def _git(bare_repo: Path, *args: str) -> str:
    proc = subprocess.run(  # - fixed argv, no shell
        ["git", "-C", str(bare_repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {bare_repo}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _find_spec_name(bare_repo: Path, ref: str, package: str) -> str | None:
    # ls-tree does not support wildcard pathspecs ("*.spec" matches nothing),
    # so list the tree and filter here instead.
    listing = _git(bare_repo, "ls-tree", "-r", "--name-only", ref)
    specs = [line for line in listing.splitlines() if line.strip().endswith(".spec")]
    if not specs:
        return None
    canonical = f"{package}.spec"
    for spec in specs:
        if Path(spec).name == canonical:
            return spec
    return specs[0]


def _spec_version_from_bare_repo(bare_repo: Path, ref: str, package: str, scratch: Path) -> str | None:
    spec_name = _find_spec_name(bare_repo, ref, package)
    if spec_name is None:
        return None
    spec_path = scratch / Path(spec_name).name
    spec_path.write_text(_git(bare_repo, "show", f"{ref}:{spec_name}"))
    return arm_inputs.read_spec_version(spec_path)


def _files_touched_by_patch(patch_text: str) -> set[str]:
    from unidiff import PatchSet

    return {patched_file.path for patched_file in PatchSet(patch_text)}


def _reference_patch_files(case: AmalgamatorCase) -> set[str] | None:
    rel = case.expected.get("reference_patch")
    if not rel:
        return None
    path = _fixtures_dir() / rel
    if not path.is_file():
        return None
    return _files_touched_by_patch(path.read_text())


def _write_result(result: dict, case: AmalgamatorCase, files: set[str]) -> Path:
    out_dir = Path(os.getenv("BACKPORT_ARTIFACTS_DIR", str(DEFAULT_ARTIFACTS_DIR))) / case.jira_issue
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "amalgamator_result.json"
    out_path.write_text(json.dumps({**result, "files_touched": sorted(files)}, indent=2) + "\n")
    return out_path


def _informational() -> bool:
    return os.getenv("AMALGAMATOR_E2E_INFORMATIONAL", "").lower() in {"1", "true", "yes"}


@pytest.mark.parametrize("case", _amalgamator_params)
def test_amalgamator_arm(case: AmalgamatorCase, mock_repos, tmp_path):
    root = _amalgamator_root()
    if root is None:
        pytest.skip("AMALGAMATOR_ROOT does not point at an Amalgamator checkout")
    interpreter = _amalgamator_interpreter()
    if os.getenv("AMALGAMATOR_PYTHON") and interpreter is None:
        pytest.skip("AMALGAMATOR_PYTHON does not point at an executable")
    if interpreter is None and sys.version_info < (3, 12):
        # Without a configured interpreter the adapter uses this one; skip
        # rather than run Amalgamator on a Python it does not support.
        pytest.skip("Amalgamator needs Python 3.12+; set AMALGAMATOR_PYTHON")

    workdir = Path(os.getenv("BACKPORT_ARTIFACTS_DIR", str(DEFAULT_ARTIFACTS_DIR))) / case.jira_issue
    workdir.mkdir(parents=True, exist_ok=True)

    config = mock_repos.get(case.jira_issue) or {}
    repos = config.get("repos") or []
    if not repos:
        pytest.skip("fixture has no dist-git repos")

    repo_info = repos[0]
    bare_repo = SHARED_BARE_REPOS_DIR / f"{case.jira_issue}-{repo_info['package']}.git"
    if arm_inputs.upstream_repo_url(case.upstream_patch_url) is None:
        pytest.skip(f"no upstream repository can be derived from {case.upstream_patch_url}")

    version = _spec_version_from_bare_repo(bare_repo, repo_info["pre_fix_ref"], case.package, tmp_path)
    if not version:
        pytest.skip("could not read the package version from the dist-git spec")

    fix_commit = arm_inputs.resolve_fix_commit(case.upstream_patch_url)
    fix_patch = None
    if fix_commit is None:
        try:
            fix_patch = arm_inputs.download_patch(
                case.upstream_patch_url, workdir / f"{case.jira_issue}-0.patch"
            )
        except RuntimeError as exc:
            pytest.skip(f"could not download the upstream fix: {exc}")

    try:
        arm = collect_inputs(
            issue_key=case.jira_issue,
            package=case.package,
            version=version,
            upstream_patch_url=case.upstream_patch_url,
            workdir=workdir,
            description=arm_inputs.read_jira_summary(case.jira_issue),
            dist_git_patch=fix_patch,
            fix_commit=fix_commit,
        )
    except ArmUnavailableError as exc:
        pytest.skip(f"Amalgamator arm not applicable: {exc}")

    result = run_arm(
        arm,
        amalgamator_root=root,
        model=os.getenv("AMALGAMATOR_MODEL") or None,
        models_path=os.getenv("AMALGAMATOR_MODELS") or None,
        timeout=int(os.getenv("AMALGAMATOR_TIMEOUT", str(adapter.DEFAULT_TIMEOUT_SECONDS))),
        python=interpreter,
    )

    files: set[str] = set()
    if result.candidate_patch and result.candidate_patch.is_file():
        # Amalgamator writes the patch as bytes because trees can carry
        # non-UTF-8 content, so decode leniently rather than raising.
        files = _files_touched_by_patch(result.candidate_patch.read_text(errors="replace"))

    result_path = _write_result({**result.to_dict(), "version": version}, case, files)
    logger.info("Amalgamator result for %s: %s (%s)", case.jira_issue, result.status, result_path)

    if result.status != "success":
        # A patch on disk is not success: Amalgamator also writes
        # candidate.patch when the pipeline stops, escalates, or only
        # partially succeeds.
        message = (
            f"Amalgamator did not succeed for {case.jira_issue} "
            f"(status={result.status}, exit={result.exit_code}, outcome={result.outcome}, "
            f"patch={'yes' if result.produced_patch else 'no'}): "
            f"{result.error or result.stderr_tail[-3:]}"
        )
        # Informational mode tolerates a writer that ran and did not succeed.
        # An adapter failure (bad config, wrong mount, timeout) means the arm
        # itself is broken and must stay visible.
        if _informational() and result.status == "failed":
            pytest.xfail(message)
        pytest.fail(message)

    reference_files = _reference_patch_files(case)
    if reference_files:
        overlap = reference_files & files
        assert overlap, (
            f"Amalgamator patch for {case.jira_issue} touches {sorted(files)}, "
            f"none of the reference files {sorted(reference_files)}"
        )
        extra = files - reference_files
        if extra:
            logger.warning("Amalgamator patch for %s touches extra files: %s", case.jira_issue, sorted(extra))
