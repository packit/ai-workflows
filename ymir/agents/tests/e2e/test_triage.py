import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from tabulate import tabulate

from ymir.agents.metrics_middleware import MetricsMiddleware
from ymir.agents.observability import setup_observability
from ymir.agents.triage_agent import TriageState, create_triage_agent, run_workflow
from ymir.common.mock_repos import (
    apply_zstream_override,
    cleanup_mock_gitconfig,
    load_all_fixture_configs,
    setup_mock_repos,
)
from ymir.common.models import BackportData, NotAffectedData, RebaseData, Resolution, TriageOutputSchema

logger = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR = Path(__file__).parent / "mock_repos" / "triage"


@dataclass
class TriageAgentTestCase:
    input: str
    expected_output: TriageOutputSchema
    metrics: dict | None = None
    finished_state: TriageState | None = None
    error: BaseException | None = None
    zstream_override: dict[str, str] | None = None

    async def run(self) -> None:
        if self.zstream_override:
            apply_zstream_override(self.zstream_override)

        metrics_middleware = MetricsMiddleware()

        def testing_factory(gateway_tools, local_tool_options=None):
            triage_agent = create_triage_agent(gateway_tools, local_tool_options)
            triage_agent.middlewares.append(metrics_middleware)
            return triage_agent

        try:
            with _span_processor.jira_issue_context(self.input):
                self.finished_state = await run_workflow(self.input, False, testing_factory)
        except BaseException as e:
            self.error = e
        finally:
            self.metrics = metrics_middleware.get_metrics()

    def __hash__(self) -> int:
        return hash(self.input)


"""
# These cases are not ready yet to be enabled. They are kept here for reference.
    TriageAgentTestCase(
        input="RHEL-61943",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="dnsmasq",
                patch_urls=[
                    "http://thekelleys.org.uk/gitweb/?p=dnsmasq.git;a=patch;h=eb1fe15ca80b6bc43cd6bfdf309ec6c590aff811"
                ],
                justification="not-implemented",
                jira_issue="RHEL-61943",
                cve_id=None,
                fix_version="rhel-8.10.z",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-29712",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="bind",
                patch_urls=[
                    "https://gitlab.isc.org/isc-projects/bind9/-/commit/7e2f50c36958f8c98d54e6d131f088a4837ce269"
                ],
                justification="not-implemented",
                jira_issue="RHEL-29712",
                cve_id=None,
                fix_version="rhel-8.10.z",
            ),
        ),
    ),
"""

test_cases = [
    TriageAgentTestCase(
        input="RHEL-15216",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="dnsmasq",
                patch_urls=[
                    "https://thekelleys.org.uk/gitweb/?p=dnsmasq.git;a=patch;h=dd33e98da09c487a58b6cb6693b8628c0b234a3b"
                ],
                justification="not-implemented",
                jira_issue="RHEL-15216",
                cve_id=None,
                fix_version="rhel-8.10.z",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-112546",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="libtiff",
                patch_urls=[
                    "https://gitlab.com/libtiff/libtiff/-/commit/3e0dcf0ec651638b2bd849b2e6f3124b36890d99.patch",
                ],
                justification="not-implemented",
                jira_issue="RHEL-112546",
                cve_id="CVE-2025-9900",
                fix_version="rhel-9.2.0.z",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-114607",
        expected_output=TriageOutputSchema(
            resolution=Resolution.REBASE,
            data=RebaseData(
                package="expat",
                version="2.7.5",
                justification="not-implemented",
                jira_issue="RHEL-114607",
                fix_version="rhel-10.2",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-177992",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="expat",
                patch_urls=[
                    "https://github.com/libexpat/libexpat/pull/1216.patch",
                ],
                justification="not-implemented",
                jira_issue="RHEL-177992",
                cve_id="CVE-2026-45186",
                fix_version="rhel-10.2.z",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-179083",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="memcached",
                patch_urls=[
                    "https://github.com/memcached/memcached/commit/d13f282b4bce33a9c33b8a1bbf07f12114160fed.patch",
                ],
                justification="not-implemented",
                jira_issue="RHEL-179083",
                cve_id="CVE-2026-47783",
                fix_version="rhel-10.2.z",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-174694",
        expected_output=TriageOutputSchema(
            resolution=Resolution.NOT_AFFECTED,
            data=NotAffectedData(
                explanation="not-implemented",
                jira_issue="RHEL-174694",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-186838",
        expected_output=TriageOutputSchema(
            resolution=Resolution.NOT_AFFECTED,
            data=NotAffectedData(
                explanation="not-implemented",
                jira_issue="RHEL-186838",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-173494",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="qt6-qtdeclarative",
                patch_urls=[
                    "https://download.qt.io/official_releases/qt/6.10/CVE-2025-14576-qtdeclarative-6.10.diff",
                ],
                justification="not-implemented",
                jira_issue="RHEL-173494",
                cve_id="CVE-2025-14576",
                fix_version="rhel-10.2.z",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-178684",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="nginx",
                patch_urls=[
                    "https://github.com/nginx/nginx/commit/ca4f92a27464ae6c2082245e4f67048c633aa032.patch",
                ],
                justification="not-implemented",
                jira_issue="RHEL-178684",
                cve_id="CVE-2026-9256",
                fix_version="rhel-9.8.z",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-152532",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="rsync",
                patch_urls=[
                    "https://github.com/RsyncProject/rsync/commit/797e17fc4a6f15e3b1756538a9f812b63942686f.patch",
                ],
                justification="not-implemented",
                jira_issue="RHEL-152532",
                cve_id="CVE-2025-10158",
                fix_version="rhel-10.1.z",
            ),
        ),
    ),
]


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
def mock_centos_stream_repos(tmp_path_factory):
    """Clone CentOS Stream RPM repos at pre-fix state for each test case.

    Bare clones are placed on the shared ``/git-repos/`` volume so that
    both the test container and the MCP gateway can access them.

    For each issue, ``setup_mock_repos`` writes a per-issue gitconfig
    (``.mock_gitconfig_{issue_key}``) as well as a shared
    ``.mock_gitconfig``.  The MCP gateway scopes ``GIT_CONFIG_GLOBAL``
    to the per-issue file via ``_meta`` on each ``call_tool`` request,
    so concurrent test cases using the same remote URL at different
    commits get the correct bare clone.

    Yields:
        Control to the test session after repos are prepared.
    """
    fixtures_dir = os.getenv("MOCK_REPOS_DIR") or str(DEFAULT_FIXTURES_DIR)
    configs = load_all_fixture_configs(fixtures_dir)

    if git_repo_basepath := os.getenv("GIT_REPO_BASEPATH"):
        repo_dir = Path(git_repo_basepath) / "e2e_mock_clones"
        shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.mkdir(parents=True, exist_ok=True)
    else:
        repo_dir = tmp_path_factory.mktemp("centos_stream_repos")

    for issue_key, config in configs.items():
        repos = config.get("repos", [])
        if not repos:
            continue

        setup_mock_repos(repos, issue_key, repo_dir)

        for tc in test_cases:
            if tc.input == issue_key:
                tc.zstream_override = config.get("zstream_override")
                break

    yield

    cleanup_mock_gitconfig()


@pytest.fixture(scope="session", autouse=True)
def run_test_cases_concurrently(request, mock_centos_stream_repos):
    """Execute selected triage test cases concurrently via asyncio.gather, then collect metrics."""
    selected = {
        item.callspec.params["test_case"]
        for item in request.session.items
        if hasattr(item, "callspec") and "test_case" in item.callspec.params
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
                test_case.input,
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


@pytest.mark.parametrize(
    "test_case",
    (pytest.param(test_case, id=test_case.input) for test_case in test_cases),
)
def test_triage_agent(test_case: TriageAgentTestCase, observability_fixture):
    if test_case.error is not None:
        raise test_case.error

    assert test_case.finished_state is not None, f"Test case {test_case.input} did not produce a result"

    real_output = test_case.finished_state.triage_result
    expected_output = test_case.expected_output
    assert real_output.resolution == expected_output.resolution
    assert real_output.data.jira_issue == expected_output.data.jira_issue

    if expected_output.resolution == Resolution.BACKPORT:
        assert real_output.data.package == expected_output.data.package
        assert real_output.data.fix_version == expected_output.data.fix_version
        assert real_output.data.patch_urls == expected_output.data.patch_urls
        assert real_output.data.cve_id == expected_output.data.cve_id
    elif expected_output.resolution == Resolution.REBASE:
        assert real_output.data.package == expected_output.data.package
        assert real_output.data.fix_version == expected_output.data.fix_version
        assert real_output.data.version == expected_output.data.version
    elif expected_output.resolution == Resolution.NOT_AFFECTED:
        assert isinstance(real_output.data, NotAffectedData)
        assert real_output.data.explanation
        assert real_output.data.justification_category
