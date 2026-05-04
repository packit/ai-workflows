"""Shared logging and credential-redaction utilities for MCP tool gateways."""

import logging
import os
import re
from typing import Any

from beeai_framework.emitter.emitter import Emitter

logger = logging.getLogger(__name__)

# Patterns that match common credential formats in log output
_REDACT_PATTERNS = [
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"),
    re.compile(r"oauth2:[^@\s]+@"),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE),
    re.compile(r"ATATT3x[A-Za-z0-9_-]{20,}"),
    re.compile(r"Basic [A-Za-z0-9+/=]{20,}"),
    re.compile(
        r"(?:token|key|password|secret|credential)[\"'=:\s]+[A-Za-z0-9+/=_-]{20,}['\"\s]*", re.IGNORECASE
    ),
]


def redact_credentials(text: str) -> str:
    """Replace credential-like patterns in text with [REDACTED]."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def setup_logging():
    """Configure logging and attach Emitter listeners that log tool calls with redacted credentials."""
    handlers = [logging.StreamHandler()]
    if debug_file := os.environ.get("DEBUG_FILE"):
        handlers.append(logging.FileHandler(debug_file))
    logging.basicConfig(level=logging.INFO, handlers=handlers)

    def on_tool_start(data: Any, meta: Any):
        logger.info(f"Tool called: {meta.creator}")
        logger.info(f"Tool arguments: {redact_credentials(str(data))}")

    def on_tool_success(data: Any, meta: Any):
        logger.info(f"Tool {meta.creator} completed successfully")

    def on_tool_error(data: Any, meta: Any):
        logger.error(f"Tool {meta.creator} failed with error: {redact_credentials(str(data))}")
        error = getattr(data, "error", None)
        if error is not None:
            logger.error(f"Tool {meta.creator} traceback:", exc_info=error)

    Emitter.root().on(re.compile(r"^tool\..+\.start$"), on_tool_start)
    Emitter.root().on(re.compile(r"^tool\..+\.success$"), on_tool_success)
    Emitter.root().on(re.compile(r"^tool\..+\.error$"), on_tool_error)
