"""
Shared configuration utilities for RHEL config management.

This module provides common functionality for loading and accessing
RHEL configuration across agents and MCP gateway.
"""

import json
import os
from pathlib import Path
from typing import Any

import aiofiles


async def load_rhel_config() -> dict[str, Any]:
    """Load RHEL configuration from rhel-config.json file.

    The file path is read from the ``RHEL_CONFIG_PATH`` environment variable,
    falling back to ``rhel-config.json`` in the current working directory.

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
            return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Error decoding {config_file}: {e}") from e
