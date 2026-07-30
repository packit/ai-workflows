import asyncio
import logging
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tabulate import tabulate

from ymir.agents.metrics_middleware import MetricsMiddleware
from ymir.agents.observability import setup_observability
from ymir.agents.reproducer_agent import ReproducerState, create_reproducer_agent, run_workflow
from ymir.common.mock_repos import (
    apply_zstream_override,
    cleanup_mock_gitconfig,
    load_all_fixture_configs,
    setup_mock_repos,
)
from ymir.common.models import ReproducerInputSchema as InputSchema
from ymir.common.utils import mcp_tools, run_tool

logger = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR = Path(__file__).parent.parent / "mock_repos" / "reproducer"

SHARED_BARE_REPOS_DIR = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / "mock_bare"


@dataclass
class VerificationResult:
    """Outcome of running the agent-created test on a single TF compose."""

    compose: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    passed: bool | None = None
    error: str | None = None


def _resolve_test_dir(test_case_input: dict, result: Any) -> Path | None:
    """Locate the test directory the agent created inside the tests clone."""
    package = result.package
    tests_clone = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / f"tests-{package}"
    if not tests_clone.is_dir():
        return None

    if getattr(result, "test_directory", None):
        relative = str(result.test_directory).strip().lstrip("/")
        if relative and ".." not in Path(relative).parts:
            candidate = tests_clone / relative
            if candidate.is_dir():
                return candidate

    cve_id = test_case_input.get("cve_id")
    if result.reproducer_type == "cve" and cve_id:
        return tests_clone / "Security" / cve_id
    return tests_clone / "Regression" / result.jira_issue


async def _verify_on_compose(
    gateway_tools: list,
    compose: str,
    test_dir: Path,
    jira_issue: str,
    package: str,
) -> VerificationResult:
    """Reserve a TF machine, copy the agent-created test, run it via tmt, and return the result."""
    vr = VerificationResult(compose=compose)
    request_id: str | None = None

    try:
        reserve = await run_tool(
            "reserve_testing_farm_machine",
            compose=compose,
            arch="x86_64",
            available_tools=gateway_tools,
        )
        request_id = reserve["id"]

        details = await run_tool(
            "get_testing_farm_reservation_details",
            request_id=request_id,
            available_tools=gateway_tools,
        )
        ssh_host = details["ssh_connection"]
        if not ssh_host or ssh_host == "not-yet-available":
            raise RuntimeError(
                f"TF reservation {request_id} on {compose} has no SSH host "
                f"(ssh_connection={ssh_host!r}, state={details.get('state')!r})"
            )

        # CTC composes often lack tmt in base repos; enable CRB+EPEL (or Copr) then install.
        # Package under test must be present for the beakerlib steps inside tmt.
        pkg = shlex.quote(package)
        install_tmt = f"""
set -euo pipefail
dnf install -y {pkg}
if command -v tmt >/dev/null 2>&1; then
  exit 0
fi
dnf install -y dnf-plugins-core || true
if ! dnf install -y tmt 'tmt+all'; then
  major="$(rpm -E %rhel 2>/dev/null || true)"
  if [ -z "$major" ] || [ "$major" = "%rhel" ]; then
    major="$(. /etc/os-release && echo "${{VERSION_ID%%.*}}")"
  fi
  dnf config-manager --set-enabled crb 2>/dev/null \\
    || dnf config-manager --set-enabled CRB 2>/dev/null \\
    || dnf config-manager --set-enabled rhel-CRB 2>/dev/null \\
    || true
  dnf install -y "https://dl.fedoraproject.org/pub/epel/epel-release-latest-${{major}}.noarch.rpm" || true
  if ! dnf install -y tmt 'tmt+all'; then
    dnf copr enable -y @teemtee/stable || dnf copr enable -y @teemtee/tmt || true
    dnf install -y tmt 'tmt+all'
  fi
fi
command -v tmt
tmt --version
""".strip()

        install = await run_tool(
            "run_remote_command",
            ssh_host=ssh_host,
            command=install_tmt,
            timeout=600,
            available_tools=gateway_tools,
        )
        if install.get("exit_code", 1) != 0:
            raise RuntimeError(
                f"Failed to install tmt (and {package}) on {compose} "
                f"(exit_code={install.get('exit_code')}):\n"
                f"stdout:\n{install.get('stdout', '')[:2000]}\n"
                f"stderr:\n{install.get('stderr', '')[:2000]}"
            )

        local_paths = [str(p) for p in test_dir.rglob("*") if p.is_file()]
        await run_tool(
            "copy_files_to_remote",
            ssh_host=ssh_host,
            local_paths=local_paths,
            remote_dir="/tmp/reproducer",
            available_tools=gateway_tools,
        )

        prepare = await run_tool(
            "run_remote_command",
            ssh_host=ssh_host,
            command=(
                "chmod +x /tmp/reproducer/*.sh && "
                "cd /tmp/reproducer && mkdir -p .fmf && echo 1 > .fmf/version"
            ),
            timeout=30,
            available_tools=gateway_tools,
        )
        if prepare.get("exit_code", 1) != 0:
            raise RuntimeError(
                f"Failed to prepare tmt metadata on {compose}:\n"
                f"stdout:\n{prepare.get('stdout', '')[:1500]}\n"
                f"stderr:\n{prepare.get('stderr', '')[:1500]}"
            )

        run_result = await run_tool(
            "run_remote_command",
            ssh_host=ssh_host,
            command="cd /tmp/reproducer && tmt --feeling-safe run --all provision --how local",
            timeout=900,
            available_tools=gateway_tools,
        )

        vr.exit_code = run_result.get("exit_code")
        vr.stdout = run_result.get("stdout", "")
        vr.stderr = run_result.get("stderr", "")
        vr.passed = vr.exit_code == 0

    except Exception as exc:
        logger.error("Verification on %s for %s failed: %s", compose, jira_issue, exc)
        vr.error = str(exc)
    finally:
        if request_id:
            try:
                await run_tool(
                    "cancel_testing_farm_request",
                    request_id=request_id,
                    available_tools=gateway_tools,
                )
            except Exception as exc:
                logger.warning("Failed to cancel TF reservation %s: %s", request_id, exc)

    return vr


class ReproducerAgentTestCase:
    def __init__(self, config: dict):
        self.input: dict = config["input"]
        self.expected: dict = config.get("expected", {})
        self.jira_issue: str = self.input["jira_issue"]
        self.verification_config: dict | None = config.get("verification")
        self.metrics: dict = None
        self.finished_state: ReproducerState | None = None
        self.error: BaseException | None = None
        self.zstream_override: dict[str, str] | None = None
        self.verification_results: dict[str, VerificationResult] = {}
        # Used by e2e/conftest.py results.yaml reporting (optional skip note).
        self.skip_reason: str | None = None

    def __repr__(self) -> str:
        return f"ReproducerTestCase({self.jira_issue})"

    async def run(self) -> None:
        if self.zstream_override:
            apply_zstream_override(self.zstream_override)

        metrics_middleware = MetricsMiddleware()

        def testing_factory(gateway_tools, local_tool_options=None, extra_middlewares=None):
            agent = create_reproducer_agent(
                gateway_tools,
                local_tool_options,
                extra_middlewares=extra_middlewares,
            )
            agent.middlewares.append(metrics_middleware)
            return agent

        input_data = InputSchema(**self.input)

        try:
            with _span_processor.jira_issue_context(self.jira_issue):
                self.finished_state = await run_workflow(
                    jira_issue=self.jira_issue,
                    dry_run=True,
                    reproducer_agent_factory=testing_factory,
                    input_data=input_data,
                )
        except BaseException as e:
            self.error = e
        finally:
            self.metrics = metrics_middleware.get_metrics()

        if self._should_run_verification():
            await self._run_verification()

    def _should_run_verification(self) -> bool:
        if not self.verification_config:
            return False
        if self.error or not self.finished_state or not self.finished_state.result:
            return False
        result = self.finished_state.result
        return result.success and not result.test_already_exists

    async def _run_verification(self) -> None:
        """Run the agent-created test on unfixed and fixed composes via independent TF machines."""
        result = self.finished_state.result
        test_dir = _resolve_test_dir(self.input, result)
        if not test_dir or not test_dir.is_dir():
            logger.warning("Cannot verify %s: test directory %s not found", self.jira_issue, test_dir)
            return

        gateway_url = os.getenv("MCP_GATEWAY_URL")
        if not gateway_url:
            logger.warning("Cannot verify %s: MCP_GATEWAY_URL not set", self.jira_issue)
            return

        async with mcp_tools(gateway_url, call_meta={"jira_issue": self.jira_issue}) as tools:
            for phase in ("unfixed", "fixed"):
                compose = self.verification_config.get(f"{phase}_compose")
                if not compose:
                    continue
                logger.info(
                    "Verification [%s] for %s: running test on %s",
                    phase,
                    self.jira_issue,
                    compose,
                )
                package = result.package or self.input.package
                self.verification_results[phase] = await _verify_on_compose(
                    tools,
                    compose,
                    test_dir,
                    self.jira_issue,
                    package,
                )


def _load_test_cases(fixtures_dir: str | Path) -> list[ReproducerAgentTestCase]:
    """Load all reproducer test case configs from the given directory."""
    configs = load_all_fixture_configs(fixtures_dir)
    cases = []
    for config in configs.values():
        if "input" not in config:
            continue
        cases.append(ReproducerAgentTestCase(config))
    return cases


test_cases = _load_test_cases(os.getenv("REPRODUCER_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR))


def _parametrize_cases():
    """Build pytest.param list."""
    return [pytest.param(tc, id=tc.jira_issue) for tc in test_cases]


_reproducer_params = _parametrize_cases()


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


@pytest.fixture(scope="session", autouse=True)
def mock_repos():
    """Clone repos at pre-fix state for each reproducer test case.

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
    fixtures_dir = os.getenv("REPRODUCER_MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR)
    configs = load_all_fixture_configs(fixtures_dir)

    if SHARED_BARE_REPOS_DIR.exists():
        shutil.rmtree(SHARED_BARE_REPOS_DIR)
    SHARED_BARE_REPOS_DIR.mkdir(parents=True, exist_ok=True)

    compose_filter_base = SHARED_BARE_REPOS_DIR.parent

    for issue_key, config in configs.items():
        repos = config.get("repos", [])
        if repos:
            setup_mock_repos(repos, issue_key, SHARED_BARE_REPOS_DIR)

        compose_filter = config.get("compose_filter", [])
        if compose_filter:
            filter_file = compose_filter_base / f".compose_filter_{issue_key}"
            filter_file.write_text(",".join(compose_filter))
            logger.info("Wrote compose filter for %s: %s", issue_key, compose_filter)

        for tc in test_cases:
            if tc.jira_issue == issue_key:
                tc.zstream_override = config.get("zstream_override")
                break

    yield

    for f in compose_filter_base.glob(".compose_filter_*"):
        f.unlink(missing_ok=True)
    cleanup_mock_gitconfig()


@pytest.fixture(scope="session", autouse=True)
def run_test_cases_concurrently(request, mock_repos):
    """Execute selected reproducer test cases concurrently via asyncio.gather, then collect metrics."""
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
    request.config.stash["metrics"] = tabulate(
        collected_metrics, ["Issue", "Agent", "Duration", "Tool Calls", "Prompt Tokens", "Completion Tokens"]
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("test_case", _reproducer_params)
def test_reproducer_agent_success(test_case: ReproducerAgentTestCase):
    """Verify the reproducer workflow completed without exceptions."""
    if test_case.error is not None:
        raise test_case.error

    assert test_case.finished_state is not None, f"Test case {test_case.jira_issue} did not produce a result"

    result = test_case.finished_state.result
    assert result is not None, f"{test_case.jira_issue}: no result on state"

    expected_success = test_case.expected.get("success", True)
    assert result.success == expected_success, (
        f"{test_case.jira_issue}: expected success={expected_success}, "
        f"got success={result.success}, summary={result.summary}"
    )


@pytest.mark.parametrize("test_case", _reproducer_params)
def test_reproducer_agent_result(test_case: ReproducerAgentTestCase):
    """Verify the reproducer output matches expected fields."""
    if test_case.error is not None:
        pytest.skip(f"Skipped because workflow errored: {test_case.error}")

    state = test_case.finished_state
    assert state is not None
    result = state.result
    assert result is not None

    expected_package = test_case.expected.get("package")
    if expected_package:
        assert result.package == expected_package, (
            f"{test_case.jira_issue}: expected package={expected_package}, got={result.package}"
        )

    expected_type = test_case.expected.get("reproducer_type")
    if expected_type:
        assert result.reproducer_type == expected_type, (
            f"{test_case.jira_issue}: expected reproducer_type={expected_type}, got={result.reproducer_type}"
        )

    expected_test_exists = test_case.expected.get("test_already_exists")
    if expected_test_exists is not None:
        assert result.test_already_exists == expected_test_exists, (
            f"{test_case.jira_issue}: expected test_already_exists={expected_test_exists}, "
            f"got={result.test_already_exists}"
        )

    expected_compose = test_case.expected.get("compose")
    if expected_compose and result.success:
        assert result.compose == expected_compose, (
            f"{test_case.jira_issue}: expected compose={expected_compose}, got={result.compose}"
        )


@pytest.mark.parametrize("test_case", _reproducer_params)
def test_reproducer_agent_artifacts(test_case: ReproducerAgentTestCase):
    """Verify that test files were created in the tests clone for successful cases."""
    if test_case.error is not None:
        pytest.skip(f"Skipped because workflow errored: {test_case.error}")

    if not test_case.expected.get("success", True):
        pytest.skip("Skipped for expected-failure test cases")

    state = test_case.finished_state
    assert state is not None
    result = state.result
    assert result is not None

    if result.test_already_exists:
        pytest.skip("Test already exists — no artifacts expected")

    if not result.success:
        pytest.skip("Reproducer was not successful — no artifacts expected")

    test_dir = _resolve_test_dir(test_case.input, result)
    assert test_dir is not None, f"{test_case.jira_issue}: could not resolve tests clone"
    assert test_dir.is_dir(), (
        f"{test_case.jira_issue}: expected test directory at {test_dir} but it does not exist"
    )

    runtest = test_dir / "runtest.sh"
    assert runtest.is_file(), f"{test_case.jira_issue}: missing runtest.sh in {test_dir}"

    main_fmf = test_dir / "main.fmf"
    assert main_fmf.is_file(), f"{test_case.jira_issue}: missing main.fmf in {test_dir}"


# ---------------------------------------------------------------------------
# Verification tests — run the agent-created test on unfixed & fixed composes
# ---------------------------------------------------------------------------


def _skip_if_no_verification(test_case: ReproducerAgentTestCase, phase: str) -> None:
    """Common skip logic for both verification tests."""
    if test_case.error is not None:
        pytest.skip(f"Skipped because workflow errored: {test_case.error}")
    if not test_case.verification_config:
        pytest.skip("No verification config in fixture")
    if not test_case.verification_config.get(f"{phase}_compose"):
        pytest.skip(f"No {phase}_compose in verification config")
    if not test_case.expected.get("success", True):
        pytest.skip("Skipped for expected-failure test cases")
    result = test_case.finished_state and test_case.finished_state.result
    if not result or not result.success or result.test_already_exists:
        pytest.skip("Agent did not create a new reproducer — verification N/A")
    if phase not in test_case.verification_results:
        pytest.skip(f"Verification phase '{phase}' did not run")


@pytest.mark.parametrize("test_case", _reproducer_params)
def test_reproducer_verification_unfixed(test_case: ReproducerAgentTestCase):
    """On an unfixed compose the reproducer test must FAIL (bug is present)."""
    _skip_if_no_verification(test_case, "unfixed")
    vr = test_case.verification_results["unfixed"]

    assert vr.error is None, (
        f"{test_case.jira_issue}: verification on unfixed compose {vr.compose} hit an error: {vr.error}"
    )
    assert vr.passed is False, (
        f"{test_case.jira_issue}: reproducer test should FAIL on unfixed compose "
        f"{vr.compose} (exit_code={vr.exit_code}), but it passed.\n"
        f"stdout:\n{vr.stdout[:2000]}\nstderr:\n{vr.stderr[:2000]}"
    )


@pytest.mark.parametrize("test_case", _reproducer_params)
def test_reproducer_verification_fixed(test_case: ReproducerAgentTestCase):
    """On a fixed compose the reproducer test must PASS (bug is resolved)."""
    _skip_if_no_verification(test_case, "fixed")
    vr = test_case.verification_results["fixed"]

    assert vr.error is None, (
        f"{test_case.jira_issue}: verification on fixed compose {vr.compose} hit an error: {vr.error}"
    )
    assert vr.passed is True, (
        f"{test_case.jira_issue}: reproducer test should PASS on fixed compose "
        f"{vr.compose} (exit_code={vr.exit_code}), but it failed.\n"
        f"stdout:\n{vr.stdout[:2000]}\nstderr:\n{vr.stderr[:2000]}"
    )
