"""
Shared configuration utilities for RHEL config management.

This module provides common functionality for loading and accessing
RHEL configuration across agents and MCP gateway.
"""

import json
import aiofiles
from pathlib import Path
from typing import Any, Dict


async def load_rhel_config() -> dict[str, Any]:
    """
    Load RHEL configuration from rhel-config.json file.

    Returns:
        Dictionary containing RHEL configuration, empty dict if file not found
    """
    config_file = "rhel-config.json"

    if not Path(config_file).exists():
        raise Exception(f"RHEL config file {config_file} not found")
    try:
        async with aiofiles.open(config_file, 'r') as f:
            content = await f.read()
            return json.loads(content)
    except json.JSONDecodeError:
        return {}
