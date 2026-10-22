"""Framework-neutral request metadata context."""

from contextvars import ContextVar
from typing import Any

request_metadata: ContextVar[Any] = ContextVar("ymir_request_metadata", default=None)
