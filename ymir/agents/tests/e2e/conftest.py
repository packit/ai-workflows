from typing import Generator


import pytest


@pytest.hookimpl(wrapper=True)
def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus, config: pytest.Config
) -> Generator:
    yield
    metrics = config.stash.get("metrics", None)

    if metrics:
        terminalreporter.write_sep("=", "Metrics")
        terminalreporter.write_line(metrics, flush=True)
