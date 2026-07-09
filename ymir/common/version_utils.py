"""
Shared version parsing utilities for RHEL version strings.

This module provides functions for parsing and comparing RHEL version
strings in various formats (e.g., rhel-9.8, rhel-9.7.z, rhel-9.0.0.z).
"""

import contextvars
import re

current_z_streams_override: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "current_z_streams_override", default=None
)


def parse_rhel_version(version: str) -> tuple[str, str, bool] | None:
    """
    Parse RHEL version string into (major, minor, is_zstream).

    Handles formats:
      - rhel-9.8        -> ("9", "8", False)
      - rhel-9.7.z      -> ("9", "7", True)
      - rhel-9.0.0.z    -> ("9", "0", True)
      - rhel-8.8.0.z    -> ("8", "8", True)
      - rhel-8.10.z     -> ("8", "10", True)

    Args:
        version: Version string like 'rhel-9.8' or 'rhel-9.7.z'

    Returns:
        Tuple of (major_version, minor_version, is_zstream) or None if parsing fails
    """
    match = re.match(r"^rhel-(\d+)\.(\d+)(?:\.0)?(\.z)?$", version.lower())
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3) is not None


def parse_zstream_branch_name(branch: str) -> tuple[str, str] | None:
    """
    Parse a Z-stream dist-git branch name into (major, minor).

    Handles formats:
      - rhel-9.7.0     -> ("9", "7")
      - rhel-10.1      -> ("10", "1")

    Args:
        branch: Branch name like 'rhel-9.7.0' or 'rhel-10.1'

    Returns:
        Tuple of (major_version, minor_version) or None if parsing fails
    """
    match = re.match(r"^rhel-(\d+)\.(\d+)(?:\.0)?$", branch.lower())
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_branch_name(branch: str) -> tuple[str, str | None] | None:
    """
    Parse any dist-git branch name into (major, minor).

    Handles formats:
      - rhel-9.7.0     -> ("9", "7")
      - rhel-10.1      -> ("10", "1")
      - rhel-10-main   -> ("10", None)
      - c9s, c10s      -> ("9", None), ("10", None)

    Args:
        branch: Branch name like 'rhel-9.7.0', 'c9s', or 'rhel-10-main'

    Returns:
        Tuple of (major_version, minor_version) or None if parsing fails.
        minor_version is None for CentOS Stream and RHEL main branches.
    """
    zstream = parse_zstream_branch_name(branch)
    if zstream:
        return zstream
    m = re.match(r"^(?:c(\d+)s|rhel-(\d+)-main)$", branch.lower())
    if not m:
        return None
    major = m.group(1) or m.group(2)
    return major, None


def construct_internal_branch_name(major_version: str, minor_version: str) -> str:
    """Construct internal RHEL branch name (e.g. rhel-9.8.0 or rhel-10.3)."""
    branch = f"rhel-{major_version}.{minor_version}"
    if int(major_version) < 10:
        branch += ".0"
    return branch


def normalize_fix_version(fix_version: str, rhel_config: dict) -> str:
    """
    Normalize a stale Y-stream fixVersion to its Z-stream equivalent.

    After a Y-stream GA (e.g. 9.8 GA → 9.9 becomes Y-stream, 9.8.z
    becomes Z-stream), some Jira issues still carry the old Y-stream
    fixVersion (rhel-9.8). This function detects that and returns
    the Z-stream form (rhel-9.8.z).

    Returns the input unchanged if it's already Z-stream, is the
    current Y-stream, or can't be parsed.
    """
    parsed = parse_rhel_version(fix_version)
    if not parsed:
        return fix_version

    major, minor, is_zstream = parsed
    if is_zstream:
        return fix_version

    y_streams = rhel_config.get("current_y_streams", {})
    if y_streams.get(major, "").lower() == fix_version.lower():
        return fix_version

    return f"rhel-{major}.{minor}.z"


def get_maintenance_majors(rhel_config: dict) -> set[str]:
    """Major versions with a Z-stream but no Y-stream (maintenance phase)."""
    current_z_streams = rhel_config.get("current_z_streams", {})
    current_y_streams = rhel_config.get("current_y_streams", {})
    return set(current_z_streams.keys()) - set(current_y_streams.keys())


async def get_maintenance_rhel_branch(branch: str) -> str | None:
    """Get internal maintenance phase RHEL branch corresponding to the given CentOS Stream branch, if any."""
    from ymir.common.base_utils import is_cs_branch
    from ymir.common.config import load_rhel_config

    if not is_cs_branch(branch):
        return None
    if not (parsed := parse_branch_name(branch)):
        return None
    major, _ = parsed

    config = await load_rhel_config()
    if major not in get_maintenance_majors(config):
        return None
    z_stream = config.get("current_z_streams", {}).get(major)
    if not z_stream or not (z_parsed := parse_rhel_version(z_stream)):
        return None
    z_major, z_minor, _ = z_parsed
    return construct_internal_branch_name(z_major, z_minor)


def get_fix_version_variants(fix_version: str) -> list[str]:
    """
    Return both Y-stream and Z-stream forms for a given fixVersion.

    During GA transitions, the same release may appear as either
    rhel-X.Y or rhel-X.Y.z. Returns both so JQL queries can match either.

    Returns [fix_version] unchanged if parsing fails.
    """
    parsed = parse_rhel_version(fix_version)
    if not parsed:
        return [fix_version]

    major, minor, _is_zstream = parsed
    return [f"rhel-{major}.{minor}", f"rhel-{major}.{minor}.z"]


async def is_older_zstream(
    version_or_branch: str,
    current_z_streams: dict[str, str] | None = None,
) -> bool:
    """
    Determine if a version string or branch name targets an older z-stream.

    An older z-stream is one whose minor version is less than the current
    z-stream minor version for the same RHEL major version.

    Accepts:
      - Fix version strings: rhel-9.6.z, rhel-9.7.z
      - Branch names: rhel-9.6.0, rhel-10.0

    Args:
        version_or_branch: Fix version string or dist-git branch name
        current_z_streams: Dict mapping major version to current z-stream
            (e.g., {"9": "rhel-9.7.z"}). If None, loaded from rhel-config.json.

    Returns:
        True if the version targets an older z-stream, False otherwise.
    """
    if current_z_streams is None:
        current_z_streams = current_z_streams_override.get()
    if current_z_streams is None:
        from ymir.common.config import load_rhel_config

        config = await load_rhel_config()
        current_z_streams = config.get("current_z_streams", {})

    # Try parsing as a z-stream version string first (rhel-9.7.z)
    parsed = parse_rhel_version(version_or_branch)
    if parsed:
        major, minor_str, is_zstream = parsed
        if not is_zstream:
            # Could be a y-stream version (rhel-9.8) or a branch name
            # that also matches the version regex (rhel-9.6.0).
            # Try branch name parsing as fallback.
            branch_parsed = parse_zstream_branch_name(version_or_branch)
            if not branch_parsed:
                # Genuine y-stream version, not an older z-stream
                return False
            major, minor_str = branch_parsed
    else:
        # Try parsing as a branch name (rhel-9.7.0)
        branch_parsed = parse_zstream_branch_name(version_or_branch)
        if not branch_parsed:
            return False
        major, minor_str = branch_parsed

    current_zstream = current_z_streams.get(major)
    if not current_zstream:
        return False

    current_parsed = parse_rhel_version(current_zstream)
    if not current_parsed:
        return False

    current_minor = int(current_parsed[1])
    target_minor = int(minor_str)
    return target_minor < current_minor
