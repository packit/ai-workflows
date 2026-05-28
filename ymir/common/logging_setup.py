"""Shared logging configuration for ymir entry points."""

import logging
from collections.abc import Iterable

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    level: int = logging.INFO,
    extra_handlers: Iterable[logging.Handler] | None = None,
) -> None:
    """Configure the root logger with timestamps and short logger names.

    Replaces any handlers already attached to the root logger so repeated
    calls (e.g. across tests) produce a consistent format.
    """
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if extra_handlers:
        handlers.extend(extra_handlers)
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)
