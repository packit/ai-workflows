import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

import git
import pytest

from ymir.agents.backport_agent import BackportState
from ymir.agents.backport_agent import run_workflow as run_backport_workflow
from ymir.agents.mr_consolidation_agent import ConsolidationState
from ymir.agents.mr_consolidation_agent import run_workflow as run_consolidation_workflow
from ymir.agents.observability import setup_observability
from ymir.agents.tests.e2e.mr_consolidation.evaluation import (
    ConsolidationArtifacts,
    ConsolidationEvaluator,
    capture_consolidation_artifacts,
)
from ymir.common.mock_repos import (
    cleanup_mock_gitconfig,
    load_all_fixture_configs,
    setup_mock_repos,
    write_mock_gitconfig,
)

logger = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR = Path(__file__).parent.parent / "mock_repos" / "mr_consolidation"
DEFAULT_ARTIFACTS_DIR = Path("/tmp/consolidation_e2e_artifacts")
SHARED_BARE_REPOS_DIR = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / "mock_bare_consolidation"


class ConsolidationTestCase:
    """End-to-end test case that runs backport agents then consolidation."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.backport_issues: list[dict] = config["backport_issues"]
        self.repos: list[dict] = config.get("repos", [])
        self.expected: dict = config.get("expected", {})
        self.backport_states: dict[str, BackportState] = {}
        self.consolidation_state: ConsolidationState | None = None
        self.artifacts: ConsolidationArtifacts | None = None
        self.error: BaseException | None = None

    def __repr__(self) -> str:
        return f"ConsolidationTestCase({self.name})"

    @property
    def has_cached_backports(self) -> bool:
        return all(cfg.get("cached_backport") and cfg.get("update_branch") for cfg in self.backport_issues)

    async def run(self) -> None:
        try:
            if self.has_cached_backports:
                self._replay_cached_backports()
            else:
                await self._run_backports()
            await self._run_consolidation()
        except BaseException as e:
            self.error = e

    def _replay_cached_backports(self) -> None:
        """Apply cached format-patches onto branches in the bare repo.

        Instead of running the full backport agent (which takes 30+ min
        per issue), replay previously-captured ``git format-patch`` output
        onto fresh branches in the shared bare repository.  The resulting
        branch layout is identical to what ``_run_backports`` would produce.
        """
        package = self.backport_issues[0]["package"]
        dist_git_branch = self.backport_issues[0]["dist_git_branch"]
        bare_repo_path = SHARED_BARE_REPOS_DIR / f"{self.name}-{package}.git"
        fixtures_dir = Path(os.getenv("MR_CONSOLIDATION_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR))

        for issue_cfg in self.backport_issues:
            patch_rel = issue_cfg["cached_backport"]
            branch_name = issue_cfg["update_branch"]
            patch_path = fixtures_dir / patch_rel

            if not patch_path.is_file():
                raise FileNotFoundError(f"Cached backport patch not found: {patch_path}")

            with tempfile.TemporaryDirectory(prefix="replay_") as work_dir:
                work_repo = git.Repo.clone_from(
                    f"file://{bare_repo_path}",
                    work_dir,
                    branch=dist_git_branch,
                )
                work_repo.git.checkout("-b", branch_name)
                work_repo.git.am(str(patch_path))
                work_repo.git.push(f"file://{bare_repo_path}", branch_name)

            logger.info(
                "Replayed cached backport onto %s for %s",
                branch_name,
                issue_cfg["jira_issue"],
            )

    async def _run_backports(self) -> None:
        """Run backport workflows concurrently and push branches to the bare repo."""
        package = self.backport_issues[0]["package"]
        bare_repo_path = SHARED_BARE_REPOS_DIR / f"{self.name}-{package}.git"

        async def _run_single(issue_cfg: dict):
            return issue_cfg, await run_backport_workflow(
                package=issue_cfg["package"],
                dist_git_branch=issue_cfg["dist_git_branch"],
                upstream_patches=issue_cfg["upstream_patches"],
                jira_issue=issue_cfg["jira_issue"],
                cve_id=issue_cfg.get("cve_id"),
                dry_run=True,
            )

        results = await asyncio.gather(*(_run_single(cfg) for cfg in self.backport_issues))

        for issue_cfg, state in results:
            self.backport_states[issue_cfg["jira_issue"]] = state

            if not (state and state.backport_result and state.backport_result.success):
                logger.error(
                    "Backport failed for %s, cannot proceed with consolidation",
                    issue_cfg["jira_issue"],
                )
                continue

            repo = git.Repo(str(state.local_clone))
            repo.git.push(f"file://{bare_repo_path}", state.update_branch)
            logger.info(
                "Pushed branch %s to bare repo for %s",
                state.update_branch,
                issue_cfg["jira_issue"],
            )

    async def _run_consolidation(self) -> None:
        """Build branch info from backport results and run the consolidation workflow."""
        if self.has_cached_backports:
            backport_branches = self._build_branches_from_cache()
        else:
            backport_branches = self._build_branches_from_states()

        if len(backport_branches) < 2:
            raise AssertionError(
                f"Need at least 2 backport branches for consolidation, got {len(backport_branches)}"
            )

        first = self.backport_issues[0]
        self.consolidation_state = await run_consolidation_workflow(
            package=first["package"],
            dist_git_branch=first["dist_git_branch"],
            release_strategy=self.config.get("release_strategy", "merged"),
            backport_branches=backport_branches,
            dry_run=True,
        )

        if self.consolidation_state:
            artifacts_dir = os.getenv("CONSOLIDATION_ARTIFACTS_DIR", str(DEFAULT_ARTIFACTS_DIR))
            self.artifacts = capture_consolidation_artifacts(
                self.consolidation_state,
                self.backport_states,
                self.backport_issues,
                Path(artifacts_dir) / self.name,
            )

    def _build_branches_from_cache(self) -> list[dict]:
        branches = []
        for cfg in self.backport_issues:
            cve_id = cfg.get("cve_id", "")
            title = f"Backport {cve_id} fix for {cfg['package']}"
            description = f"Resolves: {cfg['jira_issue']}"
            if cve_id:
                description = f"CVE: {cve_id}\n{description}"
            branches.append(
                {
                    "branch": cfg["update_branch"],
                    "title": title,
                    "description": description,
                    "jira_issues": [cfg["jira_issue"]],
                }
            )
        return branches

    def _build_branches_from_states(self) -> list[dict]:
        branches = []
        for cfg in self.backport_issues:
            jira_issue = cfg["jira_issue"]
            state = self.backport_states.get(jira_issue)
            if not state or not state.backport_result or not state.backport_result.success:
                continue
            title = (
                state.log_result.title
                if state.log_result and hasattr(state.log_result, "title")
                else f"Backport {cfg.get('cve_id', '')} fix for {cfg['package']}"
            )
            description = f"Resolves: {jira_issue}"
            if cfg.get("cve_id"):
                description = f"CVE: {cfg['cve_id']}\n{description}"
            branches.append(
                {
                    "branch": state.update_branch,
                    "title": title,
                    "description": description,
                    "jira_issues": [jira_issue],
                }
            )
        return branches


def _load_test_cases(fixtures_dir: str | Path) -> list[ConsolidationTestCase]:
    configs = load_all_fixture_configs(fixtures_dir)
    cases = []
    for name, config in configs.items():
        if "backport_issues" not in config:
            continue
        cases.append(ConsolidationTestCase(name, config))
    return cases


test_cases = _load_test_cases(os.getenv("MR_CONSOLIDATION_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR))

_span_processor = None


@pytest.fixture(scope="session", autouse=True)
def observability_fixture():
    global _span_processor
    _span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])
    yield _span_processor


@pytest.fixture(scope="session", autouse=True)
def mock_centos_stream_repos():
    """Set up bare clones shared by all backport and consolidation runs."""
    fixtures_dir = os.getenv("MR_CONSOLIDATION_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR)
    configs = load_all_fixture_configs(fixtures_dir)

    if SHARED_BARE_REPOS_DIR.exists():
        shutil.rmtree(SHARED_BARE_REPOS_DIR)
    SHARED_BARE_REPOS_DIR.mkdir(parents=True, exist_ok=True)

    for test_name, config in configs.items():
        repos = config.get("repos", [])
        if not repos:
            continue

        git_env = setup_mock_repos(repos, test_name, SHARED_BARE_REPOS_DIR)

        for issue_cfg in config.get("backport_issues", []):
            write_mock_gitconfig(git_env, issue_key=issue_cfg["jira_issue"])

        first = config["backport_issues"][0]
        consolidation_id = f"consolidation-{first['package']}-{first['dist_git_branch']}"
        write_mock_gitconfig(git_env, issue_key=consolidation_id)

    yield

    cleanup_mock_gitconfig()


@pytest.fixture(scope="session", autouse=True)
def run_test_cases(mock_centos_stream_repos):
    """Run all consolidation test cases concurrently via asyncio.gather."""

    async def _run_all():
        await asyncio.gather(*(tc.run() for tc in test_cases))

    asyncio.run(_run_all())
    yield


def _get_patch_files(repo_path: Path) -> dict[str, str]:
    """Return {filename: content} for all .patch/.diff files in a repo clone."""
    patches = {}
    for ext in ("*.patch", "*.diff"):
        for p in repo_path.glob(ext):
            patches[p.name] = p.read_text()
    return patches


def _get_backport_patch_files(state: BackportState) -> dict[str, str]:
    """Extract patch files added by the backport commit."""
    if not state.local_clone or not Path(state.local_clone).is_dir():
        return {}
    try:
        repo = git.Repo(str(state.local_clone))
        changed = repo.git.diff("HEAD~1", "HEAD", "--name-only").splitlines()
        patch_extensions = (".patch", ".diff")
        return {
            f: (Path(state.local_clone) / f).read_text()
            for f in changed
            if f.endswith(patch_extensions) and (Path(state.local_clone) / f).is_file()
        }
    except Exception:
        return {}


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.name for tc in test_cases])
def test_backports_succeeded(test_case: ConsolidationTestCase):
    """All backport runs must succeed for the consolidation test to be valid."""
    if test_case.error is not None:
        raise test_case.error

    if test_case.has_cached_backports:
        pytest.skip("Using cached backports — backport agent was not run")

    for issue_cfg in test_case.backport_issues:
        jira_issue = issue_cfg["jira_issue"]
        state = test_case.backport_states.get(jira_issue)
        assert state is not None, f"No backport result for {jira_issue}"
        assert state.backport_result is not None, f"No backport_result for {jira_issue}"
        assert state.backport_result.success, (
            f"Backport failed for {jira_issue}: {state.backport_result.error}"
        )


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.name for tc in test_cases])
def test_consolidation_succeeded(test_case: ConsolidationTestCase):
    """The consolidation workflow must complete successfully."""
    if test_case.error is not None:
        raise test_case.error

    state = test_case.consolidation_state
    assert state is not None, f"{test_case.name}: consolidation did not produce a result"
    assert state.consolidation_result is not None, f"{test_case.name}: no consolidation_result"

    expected_success = test_case.expected.get("consolidation_success", True)
    assert state.consolidation_result.success == expected_success, (
        f"{test_case.name}: expected success={expected_success}, "
        f"got success={state.consolidation_result.success}, "
        f"error={state.consolidation_result.error}"
    )


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.name for tc in test_cases])
def test_consolidated_patches_present(test_case: ConsolidationTestCase):
    """The consolidated branch must contain patches from all backport branches."""
    if test_case.error is not None:
        raise test_case.error

    state = test_case.consolidation_state
    if not state or not state.consolidation_result or not state.consolidation_result.success:
        pytest.skip("Consolidation did not succeed")

    assert state.local_clone is not None
    consolidated_patches = _get_patch_files(Path(state.local_clone))

    min_count = test_case.expected.get("min_patch_count", len(test_case.backport_issues))
    assert len(consolidated_patches) >= min_count, (
        f"{test_case.name}: expected at least {min_count} patches in consolidated result, "
        f"found {len(consolidated_patches)}: {list(consolidated_patches.keys())}"
    )


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.name for tc in test_cases])
def test_consolidated_patches_consistent(test_case: ConsolidationTestCase):
    """Patches in the consolidated branch must match the individual backport patches."""
    if test_case.error is not None:
        raise test_case.error

    state = test_case.consolidation_state
    if not state or not state.consolidation_result or not state.consolidation_result.success:
        pytest.skip("Consolidation did not succeed")

    if test_case.has_cached_backports:
        pytest.skip("Using cached backports — no live backport clones to compare against")

    assert state.local_clone is not None
    consolidated_patches = _get_patch_files(Path(state.local_clone))

    for issue_cfg in test_case.backport_issues:
        jira_issue = issue_cfg["jira_issue"]
        bp_state = test_case.backport_states.get(jira_issue)
        if not bp_state or not bp_state.backport_result or not bp_state.backport_result.success:
            continue

        backport_patches = _get_backport_patch_files(bp_state)
        for patch_name, patch_content in backport_patches.items():
            assert patch_name in consolidated_patches, (
                f"{test_case.name}: patch '{patch_name}' from {jira_issue} "
                f"is missing in the consolidated result. "
                f"Consolidated patches: {list(consolidated_patches.keys())}"
            )
            if consolidated_patches[patch_name] != patch_content:
                logger.info(
                    "%s: patch '%s' from %s was adapted during consolidation (expected when patches overlap)",
                    test_case.name,
                    patch_name,
                    jira_issue,
                )


# ---------------------------------------------------------------------------
# LLM judge tests (optional, controlled by RUN_LLM_JUDGE env var)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.name for tc in test_cases])
def test_consolidation_llm_judge(test_case: ConsolidationTestCase):
    """Verify via LLM judge that consolidated patches still fix all original issues."""
    if os.getenv("RUN_LLM_JUDGE", "").lower() != "true":
        pytest.skip("LLM judge disabled (set RUN_LLM_JUDGE=true to enable)")

    if test_case.error is not None:
        pytest.skip(f"Skipped because workflow errored: {test_case.error}")

    if not test_case.expected.get("consolidation_success", True):
        pytest.skip("Skipped for expected-failure test cases")

    artifacts = test_case.artifacts
    if artifacts is None:
        pytest.skip("No artifacts captured")

    issues_context = {}
    for cfg in test_case.backport_issues:
        issues_context[cfg["jira_issue"]] = {
            "cve_id": cfg.get("cve_id"),
            "upstream_patches": cfg.get("upstream_patches", []),
        }

    evaluator = ConsolidationEvaluator()
    context = {
        "package": test_case.backport_issues[0]["package"],
        "issues": issues_context,
    }

    verdict = asyncio.run(evaluator.evaluate(artifacts, context))

    assert verdict.passed, f"LLM judge FAILED for {test_case.name}:\n{verdict.reasoning}"
