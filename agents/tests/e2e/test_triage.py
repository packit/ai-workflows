from tabulate import tabulate
import pytest
import os

from agents.triage_agent import run_workflow, TriageState, create_triage_agent
from agents.metrics_middleware import MetricsMiddleware
from agents.observability import setup_observability
from common.models import TriageOutputSchema, Resolution, BackportData


class TriageAgentTestCase:
    def __init__(self, input, expected_output):
        self.input = input
        self.expected_output = expected_output
        self.metrics: dict = None

    async def run(self) -> TriageState:
        metrics_middleware = MetricsMiddleware()
        def testing_factory(gateway_tools):
            triage_agent = create_triage_agent(gateway_tools)
            triage_agent.middlewares.append(metrics_middleware)
            return triage_agent
        finished_state = await run_workflow(self.input, False, testing_factory)
        self.metrics = metrics_middleware.get_metrics()
        return finished_state

test_cases=[
    TriageAgentTestCase(input="RHEL-15216",
                        expected_output=TriageOutputSchema(resolution=Resolution.BACKPORT,
                                                           data=BackportData(package="dnsmasq",
                                                           patch_urls=["http://thekelleys.org.uk/gitweb/?p=dnsmasq.git;a=patch;h=dd33e98da09c487a58b6cb6693b8628c0b234a3b"],
                                                           justification="not-implemented",
                                                           jira_issue="RHEL-15216",
                                                           cve_id=None,
                                                           fix_version="rhel-8.10"))
    ),
    TriageAgentTestCase(input="RHEL-112546",
                        expected_output=TriageOutputSchema(resolution=Resolution.BACKPORT,
                                                           data=BackportData(package="libtiff",
                                                           patch_urls=["https://gitlab.com/libtiff/libtiff/-/commit/d1c0719e004fbb223c571d286c73911569d4dbb6.patch"],
                                                           justification="not-implemented",
                                                           jira_issue="RHEL-112546",
                                                           cve_id="CVE-2025-9900",
                                                           fix_version="rhel-9.6.z"))
    ),
    TriageAgentTestCase(input="RHEL-61943",
                        expected_output=TriageOutputSchema(resolution=Resolution.BACKPORT,
                                                           data=BackportData(package="dnsmasq",
                                                           patch_urls=["http://thekelleys.org.uk/gitweb/?p=dnsmasq.git;a=patch;h=eb1fe15ca80b6bc43cd6bfdf309ec6c590aff811"],
                                                           justification="not-implemented",
                                                           jira_issue="RHEL-61943",
                                                           cve_id=None,
                                                           fix_version="rhel-8.10.z"))
    ),
    TriageAgentTestCase(input="RHEL-29712",
                        expected_output=TriageOutputSchema(resolution=Resolution.BACKPORT,
                                                           data=BackportData(package="bind",
                                                           patch_urls=["https://gitlab.isc.org/isc-projects/bind9/-/commit/7e2f50c36958f8c98d54e6d131f088a4837ce269"],
                                                           justification="not-implemented",
                                                           jira_issue="RHEL-29712",
                                                           cve_id=None,
                                                           fix_version="rhel-8.10.z"))
    ),
]


@pytest.fixture(scope="session", autouse=True)
def observability_fixture():
    return setup_observability(os.environ["COLLECTOR_ENDPOINT"])


@pytest.fixture(scope="session", autouse=True)
def mydata(request):
    yield
    collected_metrics = []
    for test_case in test_cases:
        if test_case.metrics is None:
            continue
        collected_metrics.append([test_case.input] + list(test_case.metrics.values()))
    request.config.stash["metrics"] = tabulate(collected_metrics, ["Issue", "Time"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "test_case",
    test_cases,
)
async def test_triage_agent(test_case: TriageAgentTestCase):
    def verify_result(real_output: TriageOutputSchema, expected_output: TriageOutputSchema):
        assert real_output.resolution == expected_output.resolution
        assert real_output.data.package == expected_output.data.package
        assert real_output.data.patch_urls == expected_output.data.patch_urls
        assert real_output.data.jira_issue == expected_output.data.jira_issue
        assert real_output.data.cve_id == expected_output.data.cve_id
        assert real_output.data.fix_version == expected_output.data.fix_version

    finished_state = await test_case.run()
    verify_result(finished_state.triage_result, test_case.expected_output)
