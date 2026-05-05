"""
Golang rebuild utility functions.

Infrastructure utilities (auth, Redis, config) are in common/utils.py.
This module contains only domain-specific helpers for golang rebuilds.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


def extract_cves_from_text(text: str) -> list[str]:
    """Extract CVE IDs from text (e.g., ["CVE-2025-12345"])."""
    cves = re.findall(r"CVE-\d{4}-\d{4,7}", text, re.IGNORECASE)
    return list({cve.upper() for cve in cves})


def extract_rhel_version_from_text(text: str) -> str | None:
    """Extract RHEL version from text (e.g., "rhel-9.7.z")."""
    patterns = [
        r"rhel-(\d+)\.(\d+)(?:\.0)?(\.z)",
        r"(?:^|\s)(\d+)\.(\d+)(\.z)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and len(match.groups()) == 3:
            major, minor, zstream = match.groups()
            return f"rhel-{major}.{minor}{zstream}"
    return None


def format_cve_list(cves: list[str]) -> str:
    """Format list of CVEs as space-separated string."""
    return " ".join(sorted(cves))


def format_jira_list(jiras: list[str]) -> str:
    """Format list of Jira keys as space-separated string."""
    return " ".join(sorted(jiras))


def format_date_for_changelog() -> str:
    """Format current date for RPM changelog (e.g., 'Mon Apr 29 2026')."""
    return datetime.now().strftime("%a %b %d %Y")


def load_golang_config(config_path: str | None = None) -> dict[str, Any]:
    """
    Load golang rebuild configuration from YAML file.

    Args:
        config_path: Path to config.yaml. If None, tries default locations.

    Returns:
        Configuration dictionary
    """
    if config_path is None:
        possible_paths = [
            Path("config.yaml"),
            Path(__file__).parent / "config.yaml",
            Path.home() / ".config" / "golang-rebuild" / "config.yaml",
        ]
        for path in possible_paths:
            if path.exists():
                config_path = str(path)
                break
        else:
            raise FileNotFoundError(
                f"Golang rebuild config not found. Searched: {[str(p) for p in possible_paths]}"
            )

    with open(config_path) as f:
        return yaml.safe_load(f)


def get_rhel_version_config(config: dict, rhel_version: str) -> dict[str, Any]:
    """Get configuration for a specific RHEL version."""
    rhel_versions = config.get("rhel_versions", {})
    if rhel_version in rhel_versions:
        return rhel_versions[rhel_version]

    normalized = rhel_version.lower().replace("rhel-", "")
    for key, value in rhel_versions.items():
        if key.lower().replace("rhel-", "") == normalized:
            return value

    raise KeyError(f"RHEL version '{rhel_version}' not found in configuration")


def get_workspace_path(config: dict, component: str, rhel_version: str) -> Path:
    """Get workspace path for a component repository."""
    base_path = config.get("workspace", {}).get("base_path", "/tmp/golang-rebuilds")
    version_str = rhel_version.lower().replace("rhel-", "").replace(".z", "")
    return Path(base_path) / component / version_str / component
