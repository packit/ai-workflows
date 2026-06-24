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


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_SLOW_TESTS", "").lower() == "true":
        return
    skip_slow = pytest.mark.skip(reason="slow test — set RUN_SLOW_TESTS=true to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.hookimpl(wrapper=True)
def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus, config: pytest.Config
) -> Generator:
    yield
    metrics = config.stash.get("metrics", None)

    if metrics:
        terminalreporter.write_sep("=", "Metrics")
        terminalreporter.write_line(metrics, flush=True)
