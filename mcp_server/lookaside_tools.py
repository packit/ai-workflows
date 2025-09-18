import asyncio
import os
from pathlib import Path
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

from common.utils import init_kerberos_ticket, is_cs_branch
from common.validators import AbsolutePath


async def download_sources(
    dist_git_path: Annotated[AbsolutePath, Field(description="Absolute path to cloned dist-git repository")],
    package: Annotated[str, Field(description="Package name")],
    dist_git_branch: Annotated[str, Field(description="dist-git branch")],
) -> str:
    """
    Downloads sources from lookaside cache.
    """
    tool = "centpkg" if is_cs_branch(dist_git_branch) else "rhpkg"
    proc = await asyncio.create_subprocess_exec(
        tool,
        f"--name={package}",
        "--namespace=rpms",
        f"--release={dist_git_branch}",
        "sources",
        cwd=dist_git_path,
    )
    if await proc.wait():
        raise ToolError("Failed to download sources")
    return "Successfully downloaded sources from lookaside cache"


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
    if not await init_kerberos_ticket():
        raise ToolError("Failed to initialize Kerberos ticket")
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
