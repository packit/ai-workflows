import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import pytest
from tabulate import tabulate
from unidiff import PatchSet

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
    cleanup_mock_gitconfig,
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
        self.slow: bool = config.get("slow", False)
        self.metrics: dict = None
        self.finished_state: BackportState | None = None
        self.artifacts: CapturedArtifacts | None = None
        self.error: BaseException | None = None
        self.zstream_override: dict[str, str] | None = None

    def __repr__(self) -> str:
        return f"BackportTestCase({self.jira_issue})"

    async def run(self) -> None:
        if self.zstream_override:
            apply_zstream_override(self.zstream_override)

        metrics_middleware = MetricsMiddleware()

        async def testing_factory(gateway_tools, local_tool_options):
            agent = await create_backport_agent(
                gateway_tools,
                local_tool_options,
                fix_version=self.input.get("fix_version"),
            )
            agent.middlewares.append(metrics_middleware)
            return agent

        try:
            with _span_processor.jira_issue_context(self.jira_issue):
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


test_cases = _load_test_cases(os.getenv("BACKPORT_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR))


def _parametrize_cases():
    """Build pytest.param list, tagging slow cases with ``pytest.mark.slow``."""
    params = []
    for tc in test_cases:
        marks = [pytest.mark.slow] if tc.slow else []
        params.append(pytest.param(tc, id=tc.jira_issue, marks=marks))
    return params


_backport_params = _parametrize_cases()


_span_processor = None


@pytest.fixture(scope="session", autouse=True)
def observability_fixture():
    """Set up OpenTelemetry tracing for the test session.

    The returned ``AgentSpanProcessor`` is stored in the module-level
    ``_span_processor`` so that each test case can wrap its ``run_workflow``
    call with ``_span_processor.jira_issue_context(issue)`` — without this,
    spans lack the ``jira.issue`` attribute and the trace-server cannot
    index them by issue key.
    """
    global _span_processor
    _span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])
    yield _span_processor


SHARED_BARE_REPOS_DIR = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / "mock_bare"


@pytest.fixture(scope="session", autouse=True)
def mock_centos_stream_repos():
    """Clone CentOS Stream RPM repos at pre-fix state for each backport test case.

    Bare clones are placed in the shared ``/git-repos/`` volume so that both
    the test container and the MCP gateway can access them.

    For each issue, ``setup_mock_repos`` writes a per-issue gitconfig
    (``.mock_gitconfig_{issue_key}``) as well as a shared
    ``.mock_gitconfig``.  The MCP gateway scopes ``GIT_CONFIG_GLOBAL``
    to the per-issue file via ``_meta``, giving each concurrent test
    case its own ``insteadOf`` scope.

    Yields:
        Control to the test session after repos are prepared.
    """
    fixtures_dir = os.getenv("BACKPORT_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR)
    configs = load_all_fixture_configs(fixtures_dir)

    if SHARED_BARE_REPOS_DIR.exists():
        shutil.rmtree(SHARED_BARE_REPOS_DIR)
    SHARED_BARE_REPOS_DIR.mkdir(parents=True, exist_ok=True)

    for issue_key, config in configs.items():
        repos = config.get("repos", [])
        if not repos:
            continue

        setup_mock_repos(repos, issue_key, SHARED_BARE_REPOS_DIR)

        for tc in test_cases:
            if tc.jira_issue == issue_key:
                tc.zstream_override = config.get("zstream_override")
                break

    yield

    cleanup_mock_gitconfig()


def _files_touched_by_patch(patch_text: str) -> set[str]:
    """Extract the set of file paths modified by a unified diff."""
    patch_set = PatchSet(patch_text)
    return {patched_file.path for patched_file in patch_set}


def _load_reference_patch(test_case: "BackportAgentTestCase") -> str | None:
    """Load the reference patch content for a test case, if configured."""
    ref_patch_rel = test_case.expected.get("reference_patch")
    if not ref_patch_rel:
        return None
    fixtures_dir = Path(os.getenv("BACKPORT_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR))
    ref_patch_path = fixtures_dir / ref_patch_rel
    if ref_patch_path.is_file():
        return ref_patch_path.read_text()
    return None


@pytest.fixture(scope="session", autouse=True)
def run_test_cases_concurrently(request, mock_centos_stream_repos):
    """Execute selected backport test cases concurrently via asyncio.gather, then collect metrics."""
    selected = {
        item.callspec.params["test_case"]
        for item in request.session.items
        if hasattr(item, "callspec")
        and "test_case" in item.callspec.params
        and not any(item.iter_markers(name="skip"))
    }
    cases_to_run = [tc for tc in test_cases if tc in selected]

    async def _run_all():
        await asyncio.gather(*(tc.run() for tc in cases_to_run))

    asyncio.run(_run_all())

    yield

    collected_metrics = []
    for test_case in cases_to_run:
        if test_case.metrics is None:
            continue
        m = test_case.metrics
        collected_metrics.append(
            [
                test_case.jira_issue,
                m.get("agent_name", ""),
                f"{m.get('duration', 0):.0f}s",
                m.get("tool_calls", 0),
                m.get("prompt_tokens", 0),
                m.get("completion_tokens", 0),
            ]
        )
    skipped_slow = [tc for tc in test_cases if tc.slow and tc not in cases_to_run]
    collected_metrics.extend([tc.jira_issue, "(skipped — slow)", "-", "-", "-", "-"] for tc in skipped_slow)
    request.config.stash["metrics"] = tabulate(
        collected_metrics, ["Issue", "Agent", "Duration", "Tool Calls", "Prompt Tokens", "Completion Tokens"]
    )


# ---------------------------------------------------------------------------
# Deterministic tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("test_case", _backport_params)
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


@pytest.mark.parametrize("test_case", _backport_params)
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
        patch_regex = re.compile(patch_pattern)
        matching = [name for name in artifacts.patch_files if patch_regex.search(name)]
        assert matching, (
            f"{test_case.jira_issue}: expected patch matching '{patch_pattern}', "
            f"found: {list(artifacts.patch_files.keys())}"
        )


@pytest.mark.parametrize("test_case", _backport_params)
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
    patch_regex = re.compile(patch_pattern) if patch_pattern else None

    agent_patches = {
        name: content
        for name, content in artifacts.patch_files.items()
        if patch_regex and patch_regex.search(name)
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


@pytest.mark.parametrize("test_case", _backport_params)
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
