import asyncio
import fnmatch
import logging
import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import git
import pyrpkg.errors
import pyrpkg.lookaside
import pyrpkg.sources
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.common.base_utils import KerberosError, init_kerberos_ticket, is_cs_branch
from ymir.common.validators import AbsolutePath
from ymir.tools.base import CloneableTool as Tool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LookasideConfig:
    download_url: str
    upload_url: str
    hashtype: str
    namespaced: bool


_RHEL_CONFIG = _LookasideConfig(
    download_url="https://pkgs.devel.redhat.com/repo/",
    upload_url="https://pkgs.devel.redhat.com/lookaside/upload.cgi",
    hashtype="sha512",
    namespaced=True,
)

_CS_CONFIG = _LookasideConfig(
    download_url="https://sources.stream.centos.org/sources",
    upload_url="https://sources.stream.rdu2.redhat.com/lookaside/upload.cgi",
    hashtype="sha512",
    namespaced=True,
)


def _get_config(dist_git_branch: str) -> _LookasideConfig:
    return _CS_CONFIG if is_cs_branch(dist_git_branch) else _RHEL_CONFIG


def _get_cache(config: _LookasideConfig) -> pyrpkg.lookaside.CGILookasideCache:
    return pyrpkg.lookaside.CGILookasideCache(
        config.hashtype,
        config.download_url,
        config.upload_url,
    )


def _get_qualified_name(config: _LookasideConfig, package: str) -> str:
    if config.namespaced:
        return f"rpms/{package}"
    return package


def _update_gitignore(dist_git_path: Path, new_filenames: set[str]):
    gitignore_path = dist_git_path / ".gitignore"
    lines: list[str] = []
    if gitignore_path.exists():
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()

    patterns = [
        line.strip().lstrip("/") for line in lines if line.strip() and not line.strip().startswith("#")
    ]
    for filename in sorted(new_filenames):
        if not any(fnmatch.fnmatch(filename, pattern) for pattern in patterns):
            lines.append(f"/{filename}")

    gitignore_path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


async def _try_init_kerberos():
    try:
        await init_kerberos_ticket()
    except KerberosError as e:
        logger.warning("Kerberos initialization failed, continuing without it: %s", e)


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
        config = _get_config(tool_input.dist_git_branch)
        cache = _get_cache(config)
        qualified_name = _get_qualified_name(config, tool_input.package)
        dist_git_path = Path(tool_input.dist_git_path)

        sources_path = dist_git_path / "sources"
        if not sources_path.exists():
            return StringToolOutput(result="No sources file found, nothing to download")

        try:
            sources = pyrpkg.sources.SourcesFile(str(sources_path), "bsd")
        except (pyrpkg.errors.MalformedLineError, ValueError, OSError) as e:
            raise ToolError(f"Failed to parse sources file: {e}") from e

        loop = asyncio.get_running_loop()
        resolved_dist_git = dist_git_path.resolve()

        async def download_entry(entry):
            outfile = (dist_git_path / entry.file).resolve()
            try:
                relative_path = outfile.relative_to(resolved_dist_git)
                if ".git" in relative_path.parts:
                    raise ValueError("Access to .git directory is forbidden")
            except ValueError as e:
                raise ToolError(f"Invalid source filename in sources file: {entry.file}") from e
            try:
                await loop.run_in_executor(
                    None,
                    partial(
                        cache.download, qualified_name, entry.file, entry.hash, str(outfile), entry.hashtype
                    ),
                )
            except Exception as e:
                raise ToolError(f"Failed to download {entry.file}: {e}") from e

        unique_entries = []
        seen_files: set[str] = set()
        for entry in sources.entries:
            if entry.file not in seen_files:
                seen_files.add(entry.file)
                unique_entries.append(entry)

        if unique_entries:
            await asyncio.gather(*(download_entry(entry) for entry in unique_entries))

        return StringToolOutput(result="Successfully downloaded sources from lookaside cache")


class UploadSourcesToolInput(BaseModel):
    dist_git_path: AbsolutePath = Field(description="Absolute path to cloned dist-git repository")
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="dist-git branch")
    new_sources: list[str] = Field(description="List of new sources (file names) to upload")


class UploadSourcesTool(Tool[UploadSourcesToolInput, ToolRunOptions, StringToolOutput]):
    name = "upload_sources"
    description = """
    Uploads the specified sources to lookaside cache. Replaces the contents of the `sources` file
    with the new sources and updates `.gitignore` accordingly.
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

        try:
            await init_kerberos_ticket()
        except KerberosError as e:
            raise ToolError(f"Failed to initialize Kerberos ticket: {e}") from e

        config = _get_config(tool_input.dist_git_branch)
        cache = _get_cache(config)
        qualified_name = _get_qualified_name(config, tool_input.package)
        dist_git_path = Path(tool_input.dist_git_path)

        sources_path = dist_git_path / "sources"
        if not sources_path.exists():
            sources_path.touch()

        try:
            sources = pyrpkg.sources.SourcesFile(str(sources_path), "bsd")
        except (pyrpkg.errors.MalformedLineError, ValueError, OSError) as e:
            raise ToolError(f"Failed to parse sources file: {e}") from e
        sources.entries.clear()

        loop = asyncio.get_running_loop()
        resolved_dist_git = dist_git_path.resolve()
        new_filenames: set[str] = set()
        for filename in tool_input.new_sources:
            if filename in new_filenames:
                continue
            filepath = (dist_git_path / filename).resolve()
            try:
                relative_path = filepath.relative_to(resolved_dist_git)
                if ".git" in relative_path.parts:
                    raise ValueError("Access to .git directory is forbidden")
            except ValueError as e:
                raise ToolError(f"Invalid source file path: {filename}") from e
            if not filepath.is_file():
                raise ToolError(f"Source file not found: {filepath}")

            try:
                hash_value = await loop.run_in_executor(None, partial(cache.hash_file, str(filepath)))
            except Exception as e:
                raise ToolError(f"Failed to hash {filename}: {e}") from e
            try:
                await loop.run_in_executor(
                    None, partial(cache.upload, qualified_name, str(filepath), hash_value)
                )
            except pyrpkg.errors.AlreadyUploadedError:
                logger.info("%s is already present in lookaside cache", filename)
            except Exception as e:
                raise ToolError(f"Failed to upload {filename}: {e}") from e

            sources.add_entry(cache.hashtype, filename, hash_value)
            new_filenames.add(filename)

        sources.write()
        _update_gitignore(dist_git_path, new_filenames)

        repo = git.Repo(dist_git_path)
        repo.index.add(["sources", ".gitignore"])

        return StringToolOutput(result="Successfully uploaded the specified new sources to lookaside cache")
