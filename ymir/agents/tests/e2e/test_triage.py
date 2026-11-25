import asyncio
import logging
import os
import shutil
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry import baggage, context, trace
from tabulate import tabulate

from ymir.agents.observability import setup_observability
from ymir.agents.triage_agent import TriageState, create_triage_agent, run_workflow
from ymir.common.config import rhel_config_override
from ymir.common.mock_repos import (
    apply_zstream_override,
    cleanup_mock_gitconfig,
    load_all_fixture_configs,
    setup_mock_repos,
)
from ymir.common.models import (
    BackportData,
    NotAffectedData,
    PostponedData,
    RebaseData,
    RebuildData,
    Resolution,
    TriageOutputSchema,
)

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
    rhel_config_override: dict[str, Any] | None = None
    skip_reason: str | None = None

    async def run(self) -> None:
        if self.skip_reason is not None:
            return

        if self.zstream_override:
            apply_zstream_override(self.zstream_override)

        self.metrics = {"agent_name": "TriageAgent"}

        def testing_factory(gateway_tools, local_tool_options=None):
            triage_agent = create_triage_agent(gateway_tools, local_tool_options)

            async def run(prompt, **kwargs):
                start = monotonic()
                try:
                    result = await triage_agent.run(prompt, **kwargs)
                    usage = result.usage
                    self.metrics.update(
                        tool_calls=usage.tool_calls,
                        prompt_tokens=usage.input_tokens,
                        completion_tokens=usage.output_tokens,
                    )
                    return result
                finally:
                    self.metrics["duration"] = monotonic() - start

            return SimpleNamespace(run=run)

        try:
            with (
                rhel_config_override(self.rhel_config_override),
                _span_processor.jira_issue_context(self.input),
            ):
                self.finished_state = await run_workflow(self.input, False, testing_factory)
        except BaseException as e:
            self.error = e

    def __hash__(self) -> int:
        return hash(self.input)


test_cases = [
    # A TLS server must retain its rebuild for the same CVE as the butane case.
    TriageAgentTestCase(
        input="RHEL-285393",
        expected_output=TriageOutputSchema(
            resolution=Resolution.POSTPONED_DEPENDENCY,
            data=PostponedData(
                summary="Rebuild waiting for the golang fix for the TLS server vulnerability.",
                pending_issues=["RHEL-285391"],
                jira_issue="RHEL-285393",
                package="opentelemetry-collector",
                fix_version="rhel-10.3.z",
                cve_id="CVE-2026-97031",
                dependency_issue="RHEL-285391",
                dependency_component="golang",
            ),
        ),
    ),
    # The golang dependency has shipped (Fixed in Build set), so git-lfs needs a rebuild.
    TriageAgentTestCase(
        input="RHEL-287570",
        expected_output=TriageOutputSchema(
            resolution=Resolution.REBUILD,
            data=RebuildData(
                package="git-lfs",
                jira_issue="RHEL-287570",
                cve_id="CVE-2026-111943",
                fix_version="rhel-8.10.z",
                dependency_issue="RHEL-287268",
                dependency_component="golang",
            ),
        ),
    ),
    TriageAgentTestCase(
        input="RHEL-278652",
        expected_output=TriageOutputSchema(
            resolution=Resolution.NOT_AFFECTED,
            data=NotAffectedData(
                package="butane",
                jira_issue="RHEL-278652",
                cve_id="CVE-2026-97031",
                explanation="Server-side TLS vulnerability is not reachable from butane.",
                justification_category="Vulnerable Code not in Execute Path",
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
        skip_reason="it requires agent to debug / reproduce the problem",
    ),
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
        skip_reason="dnsmasq (upstream) requires auth to avoid AI scraping…",
    ),
    TriageAgentTestCase(
        input="RHEL-61943",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="dnsmasq",
                patch_urls=[
                    "https://thekelleys.org.uk/gitweb/?p=dnsmasq.git;a=patch;h=eb1fe15ca80b6bc43cd6bfdf309ec6c590aff811"
                ],
                justification="not-implemented",
                jira_issue="RHEL-61943",
                cve_id=None,
                fix_version="rhel-8.10.z",
            ),
        ),
        skip_reason=(
            "dnsmasq (upstream) requires auth to avoid AI scraping"
            " and also requires agent to debug / reproduce the problem"
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
    TriageAgentTestCase(
        input="RHEL-160675",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="squid",
                patch_urls=[
                    "https://github.com/squid-cache/squid/commit/703e07d25ca6fa11f52d20bf0bb879e22ab7481b.patch",
                ],
                justification="not-implemented",
                jira_issue="RHEL-160675",
                cve_id="CVE-2026-32748",
                fix_version="rhel-8.10.z",
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

    if os.getenv("TEMPORAL_TRIAGE_ADDRESS"):
        for case in test_cases:
            config = configs.get(case.input, {})
            case.zstream_override = config.get("zstream_override")
            case.rhel_config_override = config.get("rhel_config_override")
        yield
        return

    if git_repo_basepath := os.getenv("GIT_REPO_BASEPATH"):
        repo_dir = Path(git_repo_basepath) / "e2e_mock_clones"
        shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.mkdir(parents=True, exist_ok=True)
    else:
        repo_dir = tmp_path_factory.mktemp("centos_stream_repos")

    try:
        for issue_key, config in configs.items():
            rhel_override = config.get("rhel_config_override")

            for tc in test_cases:
                if tc.input == issue_key:
                    tc.zstream_override = config.get("zstream_override")
                    tc.rhel_config_override = rhel_override
                    break

            if not (repos := config.get("repos", [])):
                continue

            setup_mock_repos(repos, issue_key, repo_dir)

        yield
    finally:
        cleanup_mock_gitconfig()


async def run_temporal_cases(cases: list[TriageAgentTestCase], address: str) -> None:
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from temporalio.client import Client
    from temporalio.contrib.opentelemetry import TracingInterceptor
    from temporalio.testing import WorkflowEnvironment

    from ymir.agents.temporal_triage_ingress import TriageIngressWorkflow
    from ymir.agents.temporal_triage_provisioning import (
        PODMAN_LIMIT,
        PodmanTriageProvisioner,
        create_triage_control_worker,
    )
    from ymir.common.temporal_triage import TriageIntent, submit_triage_intent, triage_issue_workflow_id

    queue = f"triage-e2e-{uuid4().hex}"
    slots = asyncio.Semaphore(PODMAN_LIMIT)
    tracer = trace.get_tracer(__name__)
    client_options = {
        "plugins": [PydanticAIPlugin()],
        "interceptors": [TracingInterceptor()],
    }

    async def run(case: TriageAgentTestCase, client: Client):
        if case.skip_reason:
            return
        override = dict(case.rhel_config_override or {})
        if case.zstream_override:
            override["current_z_streams"] = override.get("current_z_streams", {}) | case.zstream_override
        start = monotonic()
        case.metrics = {"agent_name": "TemporalTriageIngress"}
        trace_context = baggage.set_baggage("jira.issue", case.input)
        trace_context = baggage.set_baggage("workflow.name", "TriageWorkflow", context=trace_context)
        trace_token = context.attach(trace_context)
        try:
            intent = TriageIntent(case.input, queue, "cli", dry_run=True, config_override=override or None)
            workflow_id = triage_issue_workflow_id(case.input)
            with tracer.start_as_current_span(
                "TriageWorkflow",
                attributes={"openinference.span.kind": "CHAIN", "temporal.workflow.id": workflow_id},
            ):
                async with slots:
                    await submit_triage_intent(client, intent, task_queue=queue)
                    handle = client.get_workflow_handle_for(TriageIngressWorkflow.run, workflow_id)
                    outcomes = await handle.result()
                outcome = next(item for item in outcomes if item.request_id == intent.request_id)
                result = outcome.preview
                assert result is not None, outcome.detail
                assert result.dry_run
                assert result.analysis is not None, result.skipped_reason
            case.finished_state = result.analysis.state
            if usage := result.analysis.triage_usage:
                case.metrics.update(
                    tool_calls=usage.tool_calls,
                    prompt_tokens=usage.input_tokens,
                    completion_tokens=usage.output_tokens,
                )
        except Exception as error:
            case.error = error
        finally:
            context.detach(trace_token)
            case.metrics["duration"] = monotonic() - start

    async with AsyncExitStack() as stack:
        if address == "local":
            if not (temporal_cli := shutil.which("temporal")):
                raise RuntimeError("The local Temporal E2E runner requires the temporal CLI")
            environment = await stack.enter_async_context(
                await WorkflowEnvironment.start_local(
                    dev_server_existing_path=temporal_cli,
                    ip="0.0.0.0",  # noqa: S104 - the CI container uses an unpublished Podman network
                    **client_options,
                )
            )
            client = environment.client
        else:
            client = await Client.connect(address, **client_options)
        provisioner = await PodmanTriageProvisioner.for_e2e(
            os.environ["YMIR_TRIAGE_CONTROLLER"],
            client.service_client.config.target_host,
            client.namespace,
            local=address == "local",
        )
        async with create_triage_control_worker(client, queue, provisioner):
            await asyncio.gather(*(run(case, client) for case in cases))


@pytest.fixture(scope="session", autouse=True)
def run_test_cases_concurrently(request, mock_centos_stream_repos, observability_fixture):
    """Execute selected triage test cases concurrently via asyncio.gather, then collect metrics.

    ``observability_fixture`` is an ordering dependency: test cases need its
    initialized span processor before their workflows start.
    """
    selected = {
        item.callspec.params["test_case"]
        for item in request.session.items
        if hasattr(item, "callspec") and "test_case" in item.callspec.params
    }
    cases_to_run = [tc for tc in test_cases if tc in selected]

    async def _run_all():
        if address := os.getenv("TEMPORAL_TRIAGE_ADDRESS"):
            await run_temporal_cases(cases_to_run, address)
        else:
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
    if test_case.skip_reason is not None:
        pytest.skip(test_case.skip_reason)

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
    elif expected_output.resolution == Resolution.REBUILD:
        assert isinstance(real_output.data, RebuildData)
        # A missing target branch or dependency would skip the buildroot verification.
        assert test_case.finished_state.target_branch
        assert not test_case.finished_state.applicability_check_skipped
        assert not test_case.finished_state.applicability_used_fallback
        assert real_output.data.package == expected_output.data.package
        assert real_output.data.fix_version == expected_output.data.fix_version
        assert real_output.data.cve_id == expected_output.data.cve_id
        assert real_output.data.dependency_issue == expected_output.data.dependency_issue
        assert real_output.data.dependency_component == expected_output.data.dependency_component
    elif expected_output.resolution == Resolution.NOT_AFFECTED:
        assert isinstance(real_output.data, NotAffectedData)
        assert real_output.data.explanation
        assert real_output.data.justification_category
        if expected_output.data.justification_category:
            assert real_output.data.justification_category == expected_output.data.justification_category
        if expected_output.data.package:
            assert real_output.data.package == expected_output.data.package
        if expected_output.data.cve_id:
            assert real_output.data.cve_id == expected_output.data.cve_id
    elif expected_output.resolution == Resolution.POSTPONED_DEPENDENCY:
        assert isinstance(real_output.data, PostponedData)
        # Retaining a rebuild because analysis failed does not verify applicability.
        assert test_case.finished_state.target_branch
        assert not test_case.finished_state.applicability_check_skipped
        assert not test_case.finished_state.applicability_used_fallback
        assert real_output.data.package == expected_output.data.package
        assert real_output.data.fix_version == expected_output.data.fix_version
        assert real_output.data.cve_id == expected_output.data.cve_id
        assert real_output.data.pending_issues == expected_output.data.pending_issues
        assert real_output.data.dependency_issue == expected_output.data.dependency_issue
        assert real_output.data.dependency_component == expected_output.data.dependency_component
