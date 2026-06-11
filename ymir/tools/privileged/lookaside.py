import asyncio
import logging
import os
import shlex

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.common.base_utils import KerberosError, init_kerberos_ticket, is_cs_branch
from ymir.common.validators import AbsolutePath
from ymir.tools.base import CloneableTool as Tool

logger = logging.getLogger(__name__)


def _pkg_cmd(package: str, dist_git_branch: str) -> list[str]:
    tool = "centpkg" if is_cs_branch(dist_git_branch) else "rhpkg"
    return [
        tool,
        f"--name={package}",
        "--namespace=rpms",
        f"--release={dist_git_branch}",
    ]


async def _try_init_kerberos():
    try:
        await init_kerberos_ticket()
    except KerberosError as e:
        logger.warning("Kerberos initialization failed, continuing without it: %s", e)


async def _run_capturing(argv: list[str], cwd: str | os.PathLike[str], fail_msg: str) -> str:
    """Run argv in cwd, capturing stdout and stderr together.

    Args:
        argv: Command and its arguments to execute.
        cwd: Working directory to run the command in.
        fail_msg: Prefix for the error message raised on failure.

    Returns:
        The captured combined output, stripped of surrounding whitespace.

    Raises:
        ToolError: If argv is empty, the command cannot be executed (e.g. cwd
            does not exist), or it exits non-zero. The error includes the
            command and a tail of its output so the real reason (e.g. a
            lookaside 404, Kerberos failure, or network error from
            rhpkg/centpkg) propagates back to the agent and Phoenix instead of
            an opaque "Failed to ..." message.
    """
    if not argv:
        raise ToolError(f"{fail_msg}: No command specified")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        if not os.path.isdir(cwd):
            raise ToolError(f"{fail_msg}: Directory '{cwd}' does not exist") from e
        raise ToolError(f"{fail_msg}: Failed to execute {argv[0]}: {e}") from e
    out, _ = await proc.communicate()
    output = out.decode(errors="replace").strip()
    if proc.returncode:
        tail = ("..." + output[-1500:]) if len(output) > 1500 else output
        raise ToolError(
            f"{fail_msg} (`{shlex.join(argv)}` exited {proc.returncode}): {tail or '<no output>'}"
        )
    return output


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
        cmd = _pkg_cmd(tool_input.package, tool_input.dist_git_branch)
        await _run_capturing([*cmd, "sources"], tool_input.dist_git_path, "Failed to download sources")
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
        cmd = _pkg_cmd(tool_input.package, tool_input.dist_git_branch)
        if not is_cs_branch(tool_input.dist_git_branch):
            cmd.extend(["--offline", "--released"])
        await _run_capturing([*cmd, "prep"], tool_input.dist_git_path, "Failed to prep sources")
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
        argv = [
            tool,
            f"--name={tool_input.package}",
            "--namespace=rpms",
            f"--release={tool_input.dist_git_branch}",
            "new-sources",
            *tool_input.new_sources,
        ]
        await _run_capturing(argv, tool_input.dist_git_path, "Failed to upload sources")
        return StringToolOutput(result="Successfully uploaded the specified new sources to lookaside cache")
