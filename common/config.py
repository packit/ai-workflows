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
        raise FileNotFoundError(f"RHEL config file {config_file} not found")
    try:
        async with aiofiles.open(config_file, 'r') as f:
            content = await f.read()
            return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Error decoding {config_file}: {e}") from e


async def get_package_instructions(package: str, operation: str = "rebase") -> str:
    """
    Get package-specific instructions for a given operation as a formatted string.

    Args:
        package: Package name (e.g., "llvm")
        operation: Operation type (e.g., "rebase", "backport")

    Returns:
        Formatted instruction string with bullet points, empty string if no instructions found
    """
    config = await load_rhel_config()
    package_instructions = config.get("package_instructions", {})
    instructions_list = package_instructions.get(package, {}).get(operation, [])

    if not instructions_list:
        return ""

    # Format as bulleted list
    return "\n".join(f"- {instruction}" for instruction in instructions_list)
