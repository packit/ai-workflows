import asyncio
import os

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from common.utils import KerberosError, init_kerberos_ticket, is_cs_branch
from common.validators import AbsolutePath


class DownloadSourcesToolInput(BaseModel):
    dist_git_path: AbsolutePath = Field(description="Absolute path to cloned dist-git repository")
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="dist-git branch")


class DownloadSourcesTool(Tool[DownloadSourcesToolInput, ToolRunOptions, StringToolOutput]):
    name = "download_sources"
    description = """
    Downloads sources from lookaside cache.
    """
    input_schema = DownloadSourcesToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "lookaside", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: DownloadSourcesToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        await _try_init_kerberos()
        proc = await asyncio.create_subprocess_exec(
            *_pkg_cmd(tool_input.package, tool_input.dist_git_branch),
            "sources",
            cwd=tool_input.dist_git_path,
        )
        if await proc.wait():
            raise ToolError("Failed to download sources")
        return StringToolOutput(result="Successfully downloaded sources from lookaside cache")


class PrepSourcesToolInput(BaseModel):
    dist_git_path: AbsolutePath = Field(description="Absolute path to cloned dist-git repository")
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="dist-git branch")


class PrepSourcesTool(Tool[PrepSourcesToolInput, ToolRunOptions, StringToolOutput]):
    name = "prep_sources"
    description = """
    Runs rpmbuild prep on the package to unpack and patch sources.
    """
    input_schema = PrepSourcesToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "lookaside", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: PrepSourcesToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        await _try_init_kerberos()
        proc = await asyncio.create_subprocess_exec(
            *_pkg_cmd(tool_input.package, tool_input.dist_git_branch),
            "prep",
            cwd=tool_input.dist_git_path,
        )
        if await proc.wait():
            raise ToolError("Failed to prep sources")
        return StringToolOutput(result="Successfully prepped sources")


class UploadSourcesToolInput(BaseModel):
    dist_git_path: AbsolutePath = Field(description="Absolute path to cloned dist-git repository")
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="dist-git branch")
    new_sources: list[str] = Field(description="List of new sources (file names) to upload")


class UploadSourcesTool(Tool[UploadSourcesToolInput, ToolRunOptions, StringToolOutput]):
    name = "upload_sources"
    description = """
    Uploads the specified sources to lookaside cache. Also updates the `sources` and `.gitignore` files
    accordingly and adds them to git index.
    """
    input_schema = UploadSourcesToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "lookaside", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: UploadSourcesToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        if os.getenv("DRY_RUN", "False").lower() == "true":
            return StringToolOutput(result="Dry run, not uploading sources (this is expected, not an error)")
        tool = "centpkg" if is_cs_branch(tool_input.dist_git_branch) else "rhpkg"
        try:
            await init_kerberos_ticket()
        except KerberosError as e:
            raise ToolError(f"Failed to initialize Kerberos ticket: {e}") from e
        proc = await asyncio.create_subprocess_exec(
            tool,
            f"--name={tool_input.package}",
            "--namespace=rpms",
            f"--release={tool_input.dist_git_branch}",
            "new-sources",
            *tool_input.new_sources,
            cwd=tool_input.dist_git_path,
        )
        if await proc.wait():
            raise ToolError("Failed to upload sources")
        return StringToolOutput(result="Successfully uploaded the specified new sources to lookaside cache")
