import asyncio
import re
from pathlib import Path
from typing import Any

import koji
from pydantic import BaseModel, Field
from specfile import Specfile
from specfile.utils import EVR
from specfile.value_parser import EnclosedMacroSubstitution, MacroSubstitution, ValueParser

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, StringToolOutput, Tool, ToolError, ToolRunOptions

from common.constants import BREWHUB_URL
from common.validators import NonEmptyString
from utils import get_absolute_path


class GetPackageInfoToolInput(BaseModel):
    spec: Path = Field(description="Path to a spec file")


class PackageInfo(BaseModel):
    """Package information extracted from spec file."""
    version: str = Field(description="Package version from Version field")
    patch_files: list[str] = Field(description="List of patch filenames in order (Patch0, Patch1, etc.)")


class GetPackageInfoToolOutput(JSONToolOutput[PackageInfo]):
    pass


class GetPackageInfoTool(Tool[GetPackageInfoToolInput, ToolRunOptions, GetPackageInfoToolOutput]):
    name = "get_package_info"
    description = """
    Extract package version and patch files from a spec file.

    Returns:
    - version: The package version (from Version: field)
    - patch_files: List of patch filenames in the order they appear (Patch0:, Patch1:, etc.)

    This is useful for determining the base version to checkout in upstream repository
    and which existing patches need to be applied before cherry-picking a new fix.
    """
    input_schema = GetPackageInfoToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "specfile", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetPackageInfoToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> GetPackageInfoToolOutput:
        spec_path = get_absolute_path(tool_input.spec, self)

        try:
            with Specfile(spec_path) as spec:
                version = spec.version
                with spec.patches() as patches:
                    patch_files = []
                    for patch in patches:
                        if patch.location:
                            patch_files.append(patch.location)

                return GetPackageInfoToolOutput(
                    result=PackageInfo(
                        version=version,
                        patch_files=patch_files
                    )
                )

        except Exception as e:
            raise ToolError(f"Failed to extract package info from {spec_path}: {e}") from e


class AddChangelogEntryToolInput(BaseModel):
    spec: Path = Field(description="Path to a spec file")
    content: list[str] = Field(
        description="""
        Content of the entry as a list of lines, maximum line length should be 80 characters,
        every paragraph should start with "- "
        """
    )


class AddChangelogEntryTool(Tool[AddChangelogEntryToolInput, ToolRunOptions, StringToolOutput]):
    name = "add_changelog_entry"
    description = """
    Adds a new changelog entry to the specified spec file.
    """
    input_schema = AddChangelogEntryToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "specfile", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: AddChangelogEntryToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        spec_path = get_absolute_path(tool_input.spec, self)
        try:
            with Specfile(spec_path) as spec:
                spec.add_changelog_entry(tool_input.content)
        except Exception as e:
            raise ToolError(f"Failed to add changelog entry: {e}") from e
        return StringToolOutput(result=f"Successfully added a new changelog entry to {spec_path}")


class UpdateReleaseToolInput(BaseModel):
    spec: Path = Field(description="Path to a spec file")
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="dist-git branch")
    rebase: bool = Field(description="Whether the Release update is done as part of a rebase")


class UpdateReleaseTool(Tool[UpdateReleaseToolInput, ToolRunOptions, StringToolOutput]):
    name = "update_release"
    description = """
    Updates the value of the `Release` field in the specified spec file.
    """
    input_schema = UpdateReleaseToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "specfile", self.name],
            creator=self,
        )

    @staticmethod
    def _process_zstream_branch(dist_git_branch: str) -> tuple[str, str] | None:
        if not (m := re.match(r"^(?P<prefix>rhel-(?P<x>\d+)\.)(?P<y>\d+)(?P<suffix>\.\d+)?$", dist_git_branch)):
            # not a Z-Stream branch
            return None
        higher_stream_candidate_tag = (
            m.group("prefix")
            # y++, up to 10 (highest RHEL minor version)
            + str(min(int(m.group("y")) + 1, 10))
            + (m.group("suffix") or "")
            + "-candidate"
        )
        return higher_stream_candidate_tag

    @staticmethod
    async def _get_latest_higher_stream_build(package: str, candidate_tag: str) -> EVR:
        builds = await asyncio.to_thread(
            koji.ClientSession(BREWHUB_URL).listTagged,
            package=package,
            tag=candidate_tag,
            latest=True,
            inherit=True,
            strict=True,
        )
        if not builds:
            raise RuntimeError(f"There are no builds of {package} in {candidate_tag}")
        [build] = builds
        return EVR(epoch=build["epoch"] or 0, version=build["version"], release=build["release"])

    @staticmethod
    async def _bump_or_reset_release(spec_path: Path, rebase: bool) -> None:
        with Specfile(spec_path) as spec:
            if rebase and not spec.has_autorelease:
                spec.release = "1"
            else:
                spec.bump_release()

    @classmethod
    async def _set_zstream_release(
        cls,
        spec_path: Path,
        package: str,
        rebase: bool,
        higher_stream_candidate_tag: str,
    ) -> None:
        latest_higher_stream_build = await cls._get_latest_higher_stream_build(package, higher_stream_candidate_tag)
        higher_stream_base_release, _ = latest_higher_stream_build.release.rsplit(".el", maxsplit=1)
        with Specfile(spec_path) as spec:
            current_release = spec.raw_release
        nodes = list(ValueParser.flatten(ValueParser.parse(current_release)))

        def find_macro(name):
            for index, node in reversed(list(enumerate(nodes))):
                if (
                    isinstance(node, (MacroSubstitution, EnclosedMacroSubstitution))
                    and node.name == name
                ):
                    return index
            return None

        autorelease_index = find_macro("autorelease")
        dist_index = find_macro("dist")
        if autorelease_index is not None:
            if rebase:
                # %autorelease present, rebase, reset the release
                release = "0%{?dist}.%{autorelease -n}"
            elif dist_index is not None and autorelease_index > dist_index:
                # %autorelease after %dist, most likely already a Z-Stream release, no change needed
                release = current_release
            else:
                # no %dist or %autorelease before it, let's create a new release based on the higher stream
                release = higher_stream_base_release + "%{?dist}.%{autorelease -n}"
        else:
            if rebase:
                # no %autorelease, rebase, reset the release
                release = "0%{?dist}.1"
            elif dist_index is None:
                # no %autorelease and no %dist, add %dist and Z-Stream counter
                release = current_release + "%{?dist}.1"
            elif dist_index + 1 < len(nodes):
                prefix = "".join(str(n) for n in nodes[: dist_index + 1])
                suffix = "".join(str(n) for n in nodes[dist_index + 1 :])
                if m := re.match(r"^\.(\d+)$", suffix):
                    # no %autorelease and existing Z-Stream counter after %dist, increase it
                    release = prefix + "." + str(int(m.group(1)) + 1)
                else:
                    # invalid Z-Stream counter, let's try to create a new release based on the higher stream
                    release = higher_stream_base_release + "%{?dist}.1"
            else:
                # no %autorelease, %dist present, add Z-Stream counter
                release = current_release + ".1"

        with Specfile(spec_path) as spec:
            spec.raw_release = release

    async def _run(
        self,
        tool_input: UpdateReleaseToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        spec_path = get_absolute_path(tool_input.spec, self)
        try:
            if not (higher_stream_candidate_tag := self._process_zstream_branch(tool_input.dist_git_branch)):
                await self._bump_or_reset_release(spec_path, tool_input.rebase)
            else:
                await self._set_zstream_release(
                    spec_path,
                    tool_input.package,
                    tool_input.rebase,
                    higher_stream_candidate_tag,
                )
        except Exception as e:
            raise ToolError(f"Failed to update release: {e}") from e
        return StringToolOutput(result=f"Successfully updated release in {spec_path}")
