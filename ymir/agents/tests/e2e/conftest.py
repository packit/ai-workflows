import logging
import os
from collections.abc import Generator

import pytest

from ymir.common.logging_setup import configure_logging

configure_logging(level=logging.INFO)
logging.getLogger("beeai").setLevel(logging.WARNING)
logging.getLogger("beeai_framework").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: marks tests as slow (skipped unless RUN_SLOW_TESTS=true)")

    # CRITICAL SAFETY CHECK: E2E tests MUST NOT write to production Jira.
    # These env vars must be explicitly set to prevent accidental writes to
    # real issues when running tests locally or in CI without proper configuration.
    #
    # MOCK_JIRA=true → uses mock file-based Jira backend instead of real API
    # DRY_RUN=true → skips all Jira writes even if MOCK_JIRA is not set
    #
    # Context: Previously, running E2E tests without these vars caused test
    # comments to be posted to production issues (e.g., RHEL-174694), creating
    # confusion about whether issues were being re-triaged by the production
    # pipeline. See: investigation of June 23 & 29, 2026 mystery comments.
    mock_jira = os.getenv("MOCK_JIRA", "").lower()
    dry_run = os.getenv("DRY_RUN", "").lower()

    if mock_jira != "true" and dry_run != "true":
        raise RuntimeError(
            "\n"
            "=" * 80 + "\n"
            "SAFETY CHECK FAILED: E2E tests MUST run with production Jira writes disabled.\n"
            "\n"
            "Set one of:\n"
            "  • MOCK_JIRA=true    (recommended: uses file-based mock Jira)\n"
            "  • DRY_RUN=true      (alternative: skips all Jira API calls)\n"
            "\n"
            "To run E2E tests safely:\n"
            "  make run-triage-agent-e2e-tests  (sets both automatically)\n"
            "\n"
            "Or manually:\n"
            "  MOCK_JIRA=true DRY_RUN=true pytest ymir/agents/tests/e2e/test_triage.py\n"
            "\n"
            "This check prevents accidental writes to production Jira issues.\n"
            "=" * 80
        )


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_SLOW_TESTS", "").lower() == "true":
        return
    skip_slow = pytest.mark.skip(reason="slow test — set RUN_SLOW_TESTS=true to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    # execute all other hooks to obtain the report object
    rep = yield
    if rep.when != "call":
        return rep

    test_case = item.callspec.params["test_case"]

    mode = "a" if os.path.exists("/home/beeai/results.yaml") else "w"
    with open("/home/beeai/results.yaml", mode, encoding="utf-8") as f:
        f.write(f'\n- name: "/{test_case.input}"\n')

        result = "fail" if rep.failed else "pass" if rep.passed else "skip"
        f.write(f'  result: "{result}"\n')

        f.write("  note:\n")
        for note, value in (
            ("Agent", test_case.metrics.get("agent_name", None)),
            ("Tool Calls", test_case.metrics.get("tool_calls", None)),
            ("Prompt Tokens", test_case.metrics.get("prompt_tokens", None)),
            ("Completion Tokens", test_case.metrics.get("completion_tokens", None)),
        ):
            if value is None:
                continue
            f.write(f'    - "{note}: {value}"\n')

        f.write("  log:\n")
        for log in (f"{test_case.input}.html", f"{test_case.input}.json"):
            f.write(f"    - {log}\n")

        minutes, seconds = divmod(int(test_case.metrics.get("duration", 0)), 60)
        hours, minutes = divmod(minutes, 60)
        f.write(f"  duration: {hours:02d}:{minutes:02d}:{seconds:02d}\n")

    return rep


@pytest.hookimpl(wrapper=True)
def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus, config: pytest.Config
) -> Generator:
    yield
    metrics = config.stash.get("metrics", None)

    if metrics:
        terminalreporter.write_sep("=", "Metrics")
        terminalreporter.write_line(metrics, flush=True)
