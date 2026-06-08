import logging
from collections.abc import Generator

import pytest

from ymir.common.logging_setup import configure_logging

configure_logging(level=logging.INFO)
logging.getLogger("beeai").setLevel(logging.WARNING)
logging.getLogger("beeai_framework").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)


@pytest.hookimpl(wrapper=True)
def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus, config: pytest.Config
) -> Generator:
    yield
    metrics = config.stash.get("metrics", None)

    if metrics:
        terminalreporter.write_sep("=", "Metrics")
        terminalreporter.write_line(metrics, flush=True)
