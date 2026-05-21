import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import pytest
from tabulate import tabulate

from ymir.agents.backport_agent import BackportState, create_backport_agent, run_workflow
from ymir.agents.metrics_middleware import MetricsMiddleware
from ymir.agents.observability import setup_observability
from ymir.agents.tests.e2e.backport_agent.artifact_capture import (
    CapturedArtifacts,
    capture_backport_artifacts,
)
from ymir.agents.tests.e2e.backport_agent.evaluation import BackportEvaluator
from ymir.common.mock_repos import (
    apply_zstream_override,
    load_all_fixture_configs,
    setup_mock_repos,
)

logger = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR = Path(__file__).parent.parent / "mock_repos" / "backport"
DEFAULT_ARTIFACTS_DIR = Path("/tmp/backport_e2e_artifacts")


class BackportAgentTestCase:
    def __init__(self, config: dict):
        self.input: dict = config["input"]
        self.expected: dict = config.get("expected", {})
        self.jira_issue: str = self.input["jira_issue"]
        self.metrics: dict = None
        self.finished_state: BackportState | None = None
        self.artifacts: CapturedArtifacts | None = None
        self.error: BaseException | None = None
        self.git_env: dict | None = None
        self.zstream_override: dict[str, str] | None = None

    def __repr__(self) -> str:
        return f"BackportTestCase({self.jira_issue})"

    async def run(self) -> None:
        if self.zstream_override:
            apply_zstream_override(self.zstream_override)

        metrics_middleware = MetricsMiddleware()

        async def testing_factory(gateway_tools, local_tool_options):
            if self.git_env:
                local_tool_options["env"] = self.git_env
            agent = await create_backport_agent(
                gateway_tools,
                local_tool_options,
                fix_version=self.input.get("fix_version"),
            )
            agent.middlewares.append(metrics_middleware)
            return agent

        try:
            self.finished_state = await run_workflow(
                package=self.input["package"],
                dist_git_branch=self.input["dist_git_branch"],
                upstream_patches=self.input["upstream_patches"],
                jira_issue=self.jira_issue,
                cve_id=self.input.get("cve_id"),
                fix_version=self.input.get("fix_version"),
                dry_run=True,
                backport_agent_factory=testing_factory,
            )
            if self.finished_state:
                artifacts_dir = os.getenv("BACKPORT_ARTIFACTS_DIR", str(DEFAULT_ARTIFACTS_DIR))
                self.artifacts = capture_backport_artifacts(self.finished_state, Path(artifacts_dir))
        except BaseException as e:
            self.error = e
        finally:
            self.metrics = metrics_middleware.get_metrics()


def _load_test_cases(fixtures_dir: str | Path) -> list[BackportAgentTestCase]:
    """Load all backport test case configs from the given directory."""
    configs = load_all_fixture_configs(fixtures_dir)
    cases = []
    for config in configs.values():
        if "input" not in config:
            continue
        cases.append(BackportAgentTestCase(config))
    return cases


test_cases = _load_test_cases(os.getenv("BACKPORT_MOCK_REPOS_DIR", str(DEFAULT_FIXTURES_DIR)))


@pytest.fixture(scope="session", autouse=True)
def observability_fixture():
    return setup_observability(os.environ["COLLECTOR_ENDPOINT"])


SHARED_BARE_REPOS_DIR = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / "mock_bare"
SHARED_GITCONFIG = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / ".mock_gitconfig"


@pytest.fixture(scope="session", autouse=True)
def mock_centos_stream_repos():
    """Clone CentOS Stream RPM repos at pre-fix state for each backport test case.

    Bare clones are placed in the shared ``/git-repos/`` volume so that both
    the test container and the MCP gateway can access them.  A
    ``.mock_gitconfig`` file with ``insteadOf`` entries is written to the same
    volume; the gateway picks it up via ``GIT_CONFIG_GLOBAL``.
    """
    fixtures_dir = os.getenv("BACKPORT_MOCK_REPOS_DIR", str(DEFAULT_FIXTURES_DIR))
    configs = load_all_fixture_configs(fixtures_dir)

    if SHARED_BARE_REPOS_DIR.exists():
        shutil.rmtree(SHARED_BARE_REPOS_DIR)
    SHARED_BARE_REPOS_DIR.mkdir(parents=True, exist_ok=True)

    per_issue_envs: list[dict[str, str]] = []

    for issue_key, config in configs.items():
        repos = config.get("repos", [])
        if not repos:
            continue

        git_env = setup_mock_repos(repos, issue_key, SHARED_BARE_REPOS_DIR)
        per_issue_envs.append(git_env)

        for tc in test_cases:
            if tc.jira_issue == issue_key:
                tc.git_env = git_env
                tc.zstream_override = config.get("zstream_override")
                break

    _write_mock_gitconfig(per_issue_envs)

    yield

    SHARED_GITCONFIG.unlink(missing_ok=True)


def _write_mock_gitconfig(envs: list[dict[str, str]]) -> None:
    """Write a gitconfig file with ``insteadOf`` entries from all test cases.

    The MCP gateway reads this via ``GIT_CONFIG_GLOBAL``.
    """
    lines: list[str] = []
    for git_env in envs:
        count = int(git_env.get("GIT_CONFIG_COUNT", "0"))
        for i in range(count):
            key = git_env.get(f"GIT_CONFIG_KEY_{i}", "")
            value = git_env.get(f"GIT_CONFIG_VALUE_{i}", "")
            if not key or not value:
                continue
            # key looks like 'url.file:///path/to.git.insteadOf'
            # gitconfig format: [url "file:///path/to.git"] insteadOf = <value>
            section_key, _, option = key.rpartition(".")
            if not section_key:
                continue
            section_name, _, section_param = section_key.partition(".")
            lines.append(f'[{section_name} "{section_param}"]')
            lines.append(f"\t{option} = {value}")
    if lines:
        SHARED_GITCONFIG.write_text("\n".join(lines) + "\n")


_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def _files_touched_by_patch(patch_text: str) -> set[str]:
    """Extract the set of file paths modified by a unified diff."""
    return {m.group(2) for m in _DIFF_FILE_RE.finditer(patch_text)}


def _load_reference_patch(test_case: "BackportAgentTestCase") -> str | None:
    """Load the reference patch content for a test case, if configured."""
    ref_patch_rel = test_case.expected.get("reference_patch")
    if not ref_patch_rel:
        return None
    fixtures_dir = Path(os.getenv("BACKPORT_MOCK_REPOS_DIR", str(DEFAULT_FIXTURES_DIR)))
    ref_patch_path = fixtures_dir / ref_patch_rel
    if ref_patch_path.is_file():
        return ref_patch_path.read_text()
    return None


@pytest.fixture(scope="session", autouse=True)
def run_test_cases_concurrently(request, mock_centos_stream_repos):
    """Execute all backport test cases concurrently via asyncio.gather, then collect metrics."""

    async def _run_all():
        await asyncio.gather(*(tc.run() for tc in test_cases))

    asyncio.run(_run_all())

    yield

    collected_metrics = []
    for test_case in test_cases:
        if test_case.metrics is None:
            continue
        collected_metrics.append([test_case.jira_issue, *test_case.metrics.values()])
    request.config.stash["metrics"] = tabulate(collected_metrics, ["Issue", "Time"])


# ---------------------------------------------------------------------------
# Deterministic tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.jira_issue for tc in test_cases])
def test_backport_agent_success(test_case: BackportAgentTestCase):
    """Verify the backport workflow completed without exceptions."""
    if test_case.error is not None:
        raise test_case.error

    assert test_case.finished_state is not None, f"Test case {test_case.jira_issue} did not produce a result"

    result = test_case.finished_state.backport_result
    assert result is not None, f"{test_case.jira_issue}: no backport_result on state"

    expected_success = test_case.expected.get("success", True)
    assert result.success == expected_success, (
        f"{test_case.jira_issue}: expected success={expected_success}, "
        f"got success={result.success}, error={result.error}"
    )


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.jira_issue for tc in test_cases])
def test_backport_agent_artifacts(test_case: BackportAgentTestCase):
    """Verify that expected artifacts were produced."""
    if test_case.error is not None:
        pytest.skip(f"Skipped because workflow errored: {test_case.error}")

    if not test_case.expected.get("success", True):
        pytest.skip("Skipped for expected-failure test cases")

    state = test_case.finished_state
    assert state is not None

    expected_package = test_case.expected.get("package", test_case.input["package"])
    assert state.package == expected_package

    if state.backport_result and state.backport_result.success:
        assert state.backport_result.srpm_path is not None, (
            f"{test_case.jira_issue}: successful backport but no SRPM path"
        )

    artifacts = test_case.artifacts
    if artifacts is None:
        pytest.skip("No artifacts captured")

    assert artifacts.commit_diff, f"{test_case.jira_issue}: no commit diff captured"
    assert artifacts.spec_content, f"{test_case.jira_issue}: no spec file captured"

    patch_pattern = test_case.expected.get("patch_file_pattern")
    if patch_pattern:
        matching = [name for name in artifacts.patch_files if patch_pattern in name]
        assert matching, (
            f"{test_case.jira_issue}: expected patch matching '{patch_pattern}', "
            f"found: {list(artifacts.patch_files.keys())}"
        )


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.jira_issue for tc in test_cases])
def test_backport_agent_patch_scope(test_case: BackportAgentTestCase):
    """Verify the agent's patch does not touch files outside the reference patch scope.

    When a reference patch is provided, the agent's generated patch must only
    modify the same source files. Extra files (CHANGELOG, documentation,
    copyright notices, etc.) indicate the agent backported more than the
    actual fix.
    """
    if test_case.error is not None:
        pytest.skip(f"Skipped because workflow errored: {test_case.error}")

    if not test_case.expected.get("success", True):
        pytest.skip("Skipped for expected-failure test cases")

    reference_text = _load_reference_patch(test_case)
    if reference_text is None:
        pytest.skip("No reference patch configured for this test case")

    artifacts = test_case.artifacts
    if artifacts is None:
        pytest.skip("No artifacts captured")

    reference_files = _files_touched_by_patch(reference_text)
    assert reference_files, "Reference patch does not touch any files — fixture error"

    patch_pattern = test_case.expected.get("patch_file_pattern", "")
    agent_patches = {
        name: content
        for name, content in artifacts.patch_files.items()
        if patch_pattern and patch_pattern in name
    }
    if not agent_patches:
        pytest.skip("No matching agent patch found to compare")

    for patch_name, patch_content in agent_patches.items():
        agent_files = _files_touched_by_patch(patch_content)
        extra_files = agent_files - reference_files
        assert not extra_files, (
            f"{test_case.jira_issue}: patch '{patch_name}' modifies files outside "
            f"the reference patch scope.\n"
            f"  Reference files: {sorted(reference_files)}\n"
            f"  Agent files:     {sorted(agent_files)}\n"
            f"  Extra files:     {sorted(extra_files)}"
        )


# ---------------------------------------------------------------------------
# LLM judge tests (optional, controlled by RUN_LLM_JUDGE env var)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("test_case", test_cases, ids=[tc.jira_issue for tc in test_cases])
def test_backport_agent_llm_judge(test_case: BackportAgentTestCase):
    """Evaluate backport quality using an LLM judge."""
    if os.getenv("RUN_LLM_JUDGE", "").lower() != "true":
        pytest.skip("LLM judge disabled (set RUN_LLM_JUDGE=true to enable)")

    if test_case.error is not None:
        pytest.skip(f"Skipped because workflow errored: {test_case.error}")

    if not test_case.expected.get("success", True):
        pytest.skip("Skipped for expected-failure test cases")

    artifacts = test_case.artifacts
    if artifacts is None:
        pytest.skip("No artifacts captured")

    evaluator = BackportEvaluator()
    context = {
        "jira_issue": test_case.jira_issue,
        "cve_id": test_case.input.get("cve_id"),
        "package": test_case.input["package"],
        "upstream_patches": test_case.input["upstream_patches"],
    }

    reference_text = _load_reference_patch(test_case)
    if reference_text:
        context["reference_patch"] = reference_text

    verdict = asyncio.run(evaluator.evaluate(artifacts, context))

    assert verdict.passed, f"LLM judge FAILED for {test_case.jira_issue}:\n{verdict.reasoning}"
