import asyncio
import logging
import os
from pathlib import Path

import pytest
from tabulate import tabulate

from ymir.agents.metrics_middleware import MetricsMiddleware
from ymir.agents.observability import setup_observability
from ymir.agents.triage_agent import TriageState, create_triage_agent, run_workflow
from ymir.common.mock_repos import (
    apply_zstream_override,
    load_all_fixture_configs,
    setup_mock_repos,
)
from ymir.common.models import BackportData, RebaseData, Resolution, TriageOutputSchema

logger = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR = Path(__file__).parent / "mock_repos" / "triage"


class TriageAgentTestCase:
    def __init__(self, input: str, expected_output: TriageOutputSchema):
        self.input: str = input
        self.expected_output: TriageOutputSchema = expected_output
        self.metrics: dict = None
        self.finished_state: TriageState | None = None
        self.error: BaseException | None = None
        self.git_env: dict | None = None
        self.zstream_override: dict[str, str] | None = None

    async def run(self) -> None:
        if self.zstream_override:
            apply_zstream_override(self.zstream_override)

        metrics_middleware = MetricsMiddleware()

        def testing_factory(gateway_tools):
            local_tool_options = {"env": self.git_env} if self.git_env else None
            triage_agent = create_triage_agent(gateway_tools, local_tool_options)
            triage_agent.middlewares.append(metrics_middleware)
            return triage_agent

        try:
            self.finished_state = await run_workflow(self.input, False, testing_factory)
        except BaseException as e:
            self.error = e
        finally:
            self.metrics = metrics_middleware.get_metrics()


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
                    "https://gitlab.com/libtiff/libtiff/-/commit/681694024846f543fe7d4821074b813cd9dccdfa.patch",
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
]


@pytest.fixture(scope="session", autouse=True)
def observability_fixture():
    return setup_observability(os.environ["COLLECTOR_ENDPOINT"])


@pytest.fixture(scope="session", autouse=True)
def mock_centos_stream_repos(tmp_path_factory):
    """Clone CentOS Stream RPM repos at pre-fix state for each test case.

    Fixture configs are loaded from ``MOCK_REPOS_DIR`` (env var) or the
    ``mock_repos/`` directory next to this test file. Each bare clone has its
    branch ref rewound to the pre-fix commit. A per-test-case env dict is
    built with ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_*`` /
    ``GIT_CONFIG_VALUE_*`` so that git's ``insteadOf`` URL rewriting
    transparently redirects the agent's git commands to the local clone.

    Yields:
        Control to the test session after repos are prepared.
    """
    fixtures_dir = os.getenv("MOCK_REPOS_DIR", str(DEFAULT_FIXTURES_DIR))
    configs = load_all_fixture_configs(fixtures_dir)
    repo_dir = tmp_path_factory.mktemp("centos_stream_repos")

    for issue_key, config in configs.items():
        repos = config.get("repos", [])
        if not repos:
            continue

        git_env = setup_mock_repos(repos, issue_key, repo_dir)

        for tc in test_cases:
            if tc.input == issue_key:
                tc.git_env = git_env
                tc.zstream_override = config.get("zstream_override")
                break

    yield


@pytest.fixture(scope="session", autouse=True)
def run_test_cases_concurrently(request, mock_centos_stream_repos):
    """Execute all triage test cases concurrently via asyncio.gather, then collect metrics."""

    async def _run_all():
        await asyncio.gather(*(tc.run() for tc in test_cases))

    asyncio.run(_run_all())

    yield

    collected_metrics = []
    for test_case in test_cases:
        if test_case.metrics is None:
            continue
        collected_metrics.append([test_case.input, *test_case.metrics.values()])
    request.config.stash["metrics"] = tabulate(collected_metrics, ["Issue", "Time"])


@pytest.mark.parametrize(
    "test_case",
    test_cases,
)
def test_triage_agent(test_case: TriageAgentTestCase):
    if test_case.error is not None:
        raise test_case.error

    assert test_case.finished_state is not None, f"Test case {test_case.input} did not produce a result"

    real_output = test_case.finished_state.triage_result
    expected_output = test_case.expected_output
    assert real_output.resolution == expected_output.resolution
    assert real_output.data.package == expected_output.data.package
    assert real_output.data.jira_issue == expected_output.data.jira_issue
    assert real_output.data.fix_version == expected_output.data.fix_version

    if expected_output.resolution == Resolution.BACKPORT:
        assert real_output.data.patch_urls == expected_output.data.patch_urls
        assert real_output.data.cve_id == expected_output.data.cve_id
    elif expected_output.resolution == Resolution.REBASE:
        assert real_output.data.version == expected_output.data.version
