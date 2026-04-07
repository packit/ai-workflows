import asyncio
import logging
import os
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


async def prep_sources(
    dist_git_path: Annotated[AbsolutePath, Field(description="Absolute path to cloned dist-git repository")],
    package: Annotated[str, Field(description="Package name")],
    dist_git_branch: Annotated[str, Field(description="dist-git branch")],
) -> str:
    """
    Runs rpmbuild prep on the package to unpack and patch sources.
    """
    await _try_init_kerberos()
    proc = await asyncio.create_subprocess_exec(
        *_pkg_cmd(package, dist_git_branch),
        "prep",
        cwd=dist_git_path,
    )
    if await proc.wait():
        raise ToolError("Failed to prep sources")
    return "Successfully prepped sources"


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
