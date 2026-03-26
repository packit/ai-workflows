import asyncio
import logging
import os
import re
import rpm
from pathlib import Path
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

from common.utils import KerberosError, init_kerberos_ticket, is_cs_branch
from common.validators import AbsolutePath

logger = logging.getLogger(__name__)


def _pkg_cmd(package: str, dist_git_branch: str) -> list[str]:
    tool = "centpkg" if is_cs_branch(dist_git_branch) else "rhpkg"
    return [tool, f"--name={package}", "--namespace=rpms", f"--release={dist_git_branch}"]


async def _try_init_kerberos():
    try:
        await init_kerberos_ticket()
    except KerberosError as e:
        logger.warning("Kerberos initialization failed, continuing without it: %s", e)


async def download_sources(
    dist_git_path: Annotated[AbsolutePath, Field(description="Absolute path to cloned dist-git repository")],
    package: Annotated[str, Field(description="Package name")],
    dist_git_branch: Annotated[str, Field(description="dist-git branch")],
) -> str:
    """
    Downloads sources from lookaside cache.
    """
    await _try_init_kerberos()
    proc = await asyncio.create_subprocess_exec(
        *_pkg_cmd(package, dist_git_branch),
        "sources",
        cwd=dist_git_path,
    )
    if await proc.wait():
        raise ToolError("Failed to download sources")
    return "Successfully downloaded sources from lookaside cache"


def _get_unpacked_sources(dist_git_path: str | Path, package: str) -> str:
    """
    Get the path to the root of the extracted archive directory tree
    after rpmbuild prep has been run.

    Reads the spec file to extract Name, Version, macro definitions, and the
    %setup/%autosetup -n argument. Uses rpm.expandMacro() to resolve any macros
    without requiring full spec parsing (which can fail on specs with deprecated
    syntax like %patchN).

    RPM 4.20+ creates a per-build directory named %{NAME}-%{VERSION}-build
    under _builddir (hardcoded in librpmbuild) and unpacks sources inside it.
    """
    spec_path = Path(dist_git_path) / f"{package}.spec"
    spec_text = spec_path.read_text()

    rpm.reloadConfig()
    for line in spec_text.splitlines():
        line = line.strip()
        m = re.match(r"^%(define|global)\s+(\S+)\s+(.*)", line)
        if m:
            rpm.addMacro(m.group(2), m.group(3))
        elif m := re.match(r"^Name:\s*(\S+)", line):
            rpm.addMacro("name", m.group(1))
        elif m := re.match(r"^Version:\s*(\S+)", line):
            rpm.addMacro("version", m.group(1))

    # Determine buildsubdir: use -n argument from %setup/%autosetup if present,
    # otherwise default to %{name}-%{version}
    setup_match = re.search(
        r"^%(?:auto)?setup\b.*?-n\s+(\S+)", spec_text, re.MULTILINE
    )
    if setup_match:
        buildsubdir = rpm.expandMacro(setup_match.group(1))
    else:
        buildsubdir = rpm.expandMacro("%{name}-%{version}")

    name = rpm.expandMacro("%{name}")
    version = rpm.expandMacro("%{version}")
    per_build_dir = Path(dist_git_path) / f"{name}-{version}-build"

    sources_dir = per_build_dir / buildsubdir
    if sources_dir.is_dir():
        return str(sources_dir)
    raise ToolError(f"Unpacked source directory does not exist: {sources_dir}")


async def prep_sources(
    dist_git_path: Annotated[AbsolutePath, Field(description="Absolute path to cloned dist-git repository")],
    package: Annotated[str, Field(description="Package name")],
    dist_git_branch: Annotated[str, Field(description="dist-git branch")],
) -> str:
    """
    Runs rpmbuild prep on the package to unpack and patch sources.
    Returns the absolute path to the unpacked source directory.
    """
    await _try_init_kerberos()
    proc = await asyncio.create_subprocess_exec(
        *_pkg_cmd(package, dist_git_branch),
        "prep",
        cwd=dist_git_path,
    )
    if await proc.wait():
        raise ToolError("Failed to prep sources")
    return _get_unpacked_sources(dist_git_path, package)


async def upload_sources(
    dist_git_path: Annotated[AbsolutePath, Field(description="Absolute path to cloned dist-git repository")],
    package: Annotated[str, Field(description="Package name")],
    dist_git_branch: Annotated[str, Field(description="dist-git branch")],
    new_sources: Annotated[list[str], Field(description="List of new sources (file names) to upload")],
) -> str:
    """
    Uploads the specified sources to lookaside cache. Also updates the `sources` and `.gitignore` files
    accordingly and adds them to git index.
    """
    if os.getenv("DRY_RUN", "False").lower() == "true":
        return "Dry run, not uploading sources (this is expected, not an error)"
    tool = "centpkg" if is_cs_branch(dist_git_branch) else "rhpkg"
    try:
        await init_kerberos_ticket()
    except KerberosError as e:
        raise ToolError(f"Failed to initialize Kerberos ticket: {e}") from e
    proc = await asyncio.create_subprocess_exec(
        tool,
        f"--name={package}",
        "--namespace=rpms",
        f"--release={dist_git_branch}",
        "new-sources",
        *new_sources,
        cwd=dist_git_path,
    )
    if await proc.wait():
        raise ToolError("Failed to upload sources")
    return "Successfully uploaded the specified new sources to lookaside cache"
