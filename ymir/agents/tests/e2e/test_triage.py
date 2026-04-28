import asyncio
import logging
import os
import shutil
import subprocess

import pytest
from tabulate import tabulate

from ymir.agents.metrics_middleware import MetricsMiddleware
from ymir.agents.observability import setup_observability
from ymir.agents.triage_agent import TriageState, create_triage_agent, run_workflow
from ymir.common.models import BackportData, Resolution, TriageOutputSchema

logger = logging.getLogger(__name__)

# Per-test-case CentOS Stream RPM repo fixtures.
# Each entry maps a Jira issue key to a list of repos that should be cloned
# and reset to a pre-fix commit so the agent cannot "cheat" by finding the
# already-applied backport.
REPO_FIXTURES = {
    "RHEL-15216": [
        {
            "package": "dnsmasq",
            "remote_url": "https://gitlab.com/redhat/centos-stream/rpms/dnsmasq",
            "pre_fix_ref": "8a2a7d987c18aecc60c0757b6e47200ba89f3940",  # pragma: allowlist secret
            "branch": "c8s",
        },
    ],
    "RHEL-112546": [
        {
            "package": "libtiff",
            "remote_url": "https://gitlab.com/redhat/centos-stream/rpms/libtiff",
            "pre_fix_ref": "1d8f0e982d3beff79b63559640b7bd578109ceaf",  # pragma: allowlist secret
            "branch": "c9s",
        },
    ],
    "RHEL-61943": [
        {
            "package": "dnsmasq",
            "remote_url": "https://gitlab.com/redhat/centos-stream/rpms/dnsmasq",
            "pre_fix_ref": "29f30a06a4be3f9af277e049b9f754ae58451306",  # pragma: allowlist secret
            "branch": "c8s",
        },
    ],
    "RHEL-29712": [
        {
            "package": "bind",
            "remote_url": "https://gitlab.com/redhat/centos-stream/rpms/bind",
            "pre_fix_ref": "f523ee34fdb30075a28daf6b8a72f2aed52eb80e",  # pragma: allowlist secret
            "branch": "c8s",
        },
    ],
}


class TriageAgentTestCase:
    def __init__(self, input: str, expected_output: TriageOutputSchema):
        self.input: str = input
        self.expected_output: TriageOutputSchema = expected_output
        self.metrics: dict = None
        self.finished_state: TriageState | None = None
        self.error: BaseException | None = None
        self.git_env: dict | None = None

    async def run(self) -> None:
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


test_cases = [
    TriageAgentTestCase(
        input="RHEL-15216",
        expected_output=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="dnsmasq",
                patch_urls=[
                    "http://thekelleys.org.uk/gitweb/?p=dnsmasq.git;a=patch;h=dd33e98da09c487a58b6cb6693b8628c0b234a3b"
                ],
                justification="not-implemented",
                jira_issue="RHEL-15216",
                cve_id=None,
                fix_version="rhel-8.10",
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
                    "https://gitlab.com/libtiff/libtiff/-/commit/d1c0719e004fbb223c571d286c73911569d4dbb6.patch"
                ],
                justification="not-implemented",
                jira_issue="RHEL-112546",
                cve_id="CVE-2025-9900",
                fix_version="rhel-9.6.z",
            ),
        ),
    ),
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
]


@pytest.fixture(scope="session", autouse=True)
def observability_fixture():
    return setup_observability(os.environ["COLLECTOR_ENDPOINT"])


@pytest.fixture(scope="session", autouse=True)
def mock_centos_stream_repos(tmp_path_factory):
    """Clone CentOS Stream RPM repos at pre-fix state, one per (test_case, package).

    Each bare clone has its branch ref rewound to the pre-fix commit.  A per-test-case
    env dict is built with GIT_CONFIG_COUNT / GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_*
    so that git's ``insteadOf`` URL rewriting transparently redirects the agent's
    git commands to the local clone.
    """
    repo_dir = tmp_path_factory.mktemp("centos_stream_repos")

    for issue_key, repos in REPO_FIXTURES.items():
        git_env: dict[str, str] = {}
        for i, repo_info in enumerate(repos):
            local_path = repo_dir / f"{issue_key}-{repo_info['package']}.git"
            logger.info(
                "Cloning %s (bare) into %s for %s",
                repo_info["remote_url"],
                local_path,
                issue_key,
            )
            subprocess.run(
                ["git", "clone", "--bare", repo_info["remote_url"], str(local_path)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    f"refs/heads/{repo_info['branch']}",
                    repo_info["pre_fix_ref"],
                ],
                cwd=str(local_path),
                check=True,
            )
            git_env[f"GIT_CONFIG_KEY_{i}"] = f"url.file://{local_path}.insteadOf"
            git_env[f"GIT_CONFIG_VALUE_{i}"] = repo_info["remote_url"]

        git_env["GIT_CONFIG_COUNT"] = str(len(repos))

        blocked_urls = [r["remote_url"] for r in repos]
        wrapper_dir = repo_dir / f"{issue_key}-wrappers"
        wrapper_dir.mkdir()
        for cmd in ["curl", "wget"]:
            real_path = shutil.which(cmd)
            if not real_path:
                continue
            blocked_patterns = " ".join(f'"{url}"' for url in blocked_urls)
            wrapper = wrapper_dir / cmd
            wrapper.write_text(
                f"#!/bin/bash\n"
                f"BLOCKED=({blocked_patterns})\n"
                f'for arg in "$@"; do\n'
                f'  for b in "${{BLOCKED[@]}}"; do\n'
                f'    if [[ "$arg" == "$b"* ]]; then\n'
                f'      echo "BLOCKED: $b is mocked locally; use git commands instead of curl/wget" >&2\n'
                f"      exit 1\n"
                f"    fi\n"
                f"  done\n"
                f"done\n"
                f'exec {real_path} "$@"\n'
            )
            wrapper.chmod(0o755)
        git_env["PATH"] = f"{wrapper_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

        for tc in test_cases:
            if tc.input == issue_key:
                tc.git_env = git_env
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
    assert real_output.data.patch_urls == expected_output.data.patch_urls
    assert real_output.data.jira_issue == expected_output.data.jira_issue
    assert real_output.data.cve_id == expected_output.data.cve_id
    assert real_output.data.fix_version == expected_output.data.fix_version
