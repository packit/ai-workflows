"""
Shared configuration utilities for RHEL config management.

This module provides common functionality for loading and accessing
RHEL configuration across agents and MCP gateway.
"""

import json
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import Any

import aiofiles

# Actual config used inside the agent process
current_rhel_config_override: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_rhel_config_override", default=None
)


def merge_rhel_config(base_config: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return a recursive merge of a RHEL config and a fixture override."""
    merged = deepcopy(dict(base_config))
    for key, value in override.items():
        if isinstance(current_value := merged.get(key), Mapping) and isinstance(value, Mapping):
            merged[key] = merge_rhel_config(current_value, value)
        else:
            merged[key] = deepcopy(value)
    return merged


@contextmanager
def rhel_config_override(override: Mapping[str, Any] | None) -> Generator[None, None, None]:
    """Temporarily apply a task-local RHEL config override.

    E2E test cases run concurrently, so an environment-variable override
    would leak between workflows. ``ContextVar`` keeps an override local to
    the current async task and any child tasks it creates. MCP tool sessions
    forward this override in request metadata to the gateway.
    """
    if override is not None and not isinstance(override, Mapping):
        raise ValueError("rhel_config_override must be a JSON object")
    token = current_rhel_config_override.set(deepcopy(dict(override)) if override is not None else None)
    try:
        yield
    finally:
        current_rhel_config_override.reset(token)


def _mcp_rhel_config_override() -> dict[str, Any] | None:
    """Read a permitted per-test E2E override from MCP request metadata."""
    if os.getenv("ALLOW_RHEL_CONFIG_OVERRIDE", "").lower() != "true":
        return None

    from ymir.common.request_context import request_metadata

    meta = request_metadata.get()
    if meta is None:
        try:
            from ymir.tools.mcp_context import request_meta
        except ImportError:
            request_meta = None
        if request_meta is not None:
            meta = request_meta()
    if meta is None:
        return None

    extra = getattr(meta, "__pydantic_extra__", None) or {}
    if (override := getattr(meta, "rhel_config_override", None)) is None:
        override = extra.get("rhel_config_override")
    if override is None:
        return None
    if not isinstance(override, dict):
        raise ValueError("rhel_config_override must be a JSON object")
    return override


async def load_rhel_config() -> dict[str, Any]:
    """Load RHEL configuration from rhel-config.json file.

    The file path is read from the ``RHEL_CONFIG_PATH`` environment variable,
    falling back to ``rhel-config.json`` in the current working directory.
    Optional fixture overrides are merged recursively: dictionaries retain
    unspecified entries, while lists and scalar values are replaced.

    Returns:
        Dictionary containing RHEL configuration.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config file contains invalid JSON.
    """
    config_file = os.environ.get("RHEL_CONFIG_PATH", "rhel-config.json")

    if not Path(config_file).exists():
        raise FileNotFoundError(f"RHEL config file {config_file} not found")
    try:
        async with aiofiles.open(config_file) as f:
            content = await f.read()
            config = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Error decoding {config_file}: {e}") from e

    if (override := current_rhel_config_override.get()) is None:
        override = _mcp_rhel_config_override()
    return merge_rhel_config(config, override) if override else config
