import asyncio
import re
from pathlib import Path

import koji
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import (
    JSONToolOutput,
    StringToolOutput,
    ToolError,
    ToolRunOptions,
)
from pydantic import BaseModel, Field
from specfile import Specfile
from specfile.prep import AutopatchMacro, AutosetupMacro, PatchMacro
from specfile.utils import EVR
from specfile.value_parser import (
    EnclosedMacroSubstitution,
    MacroSubstitution,
    Node,
    ValueParser,
)

from ymir.common.utils import get_absolute_path
from ymir.tools.base import CloneableTool as Tool
from ymir.tools.constants import BREWHUB_URL


class GetPackageInfoToolInput(BaseModel):
    spec: Path = Field(description="Path to a spec file")


class PackageInfo(BaseModel):
    """Package information extracted from spec file."""

    version: str = Field(description="Package version from Version field")
    patch_files: list[str] = Field(description="List of patch filenames in order (Patch0, Patch1, etc.)")
    patch_strip_levels: dict[str, int] = Field(
        description="Mapping of patch filename to its strip level (-p value) from the spec's %prep section"
    )


class GetPackageInfoToolOutput(JSONToolOutput[PackageInfo]):
    pass


_DEFAULT_STRIP_LEVEL = 1


def _extract_strip_levels(spec: Specfile, number_to_filename: dict[int, str]) -> dict[str, int]:
    """Build a mapping of patch filename to strip level from %prep macros.

    Handles %autosetup (global -p), %autopatch (global -p), and
    individual %patch macros (per-patch -p).  Falls back to 1 for any
    patch not covered by a macro (e.g. conditionally applied patches).
    """
    strip_levels: dict[str, int] = {}

    try:
        with spec.prep() as prep:
            for macro in prep.macros:
                if isinstance(macro, (AutosetupMacro, AutopatchMacro)):
                    p = macro.options.get("p")
                    level = p if isinstance(p, int) else _DEFAULT_STRIP_LEVEL
                    for filename in number_to_filename.values():
                        strip_levels[filename] = level
                elif isinstance(macro, PatchMacro):
                    p = macro.options.get("p")
                    level = p if isinstance(p, int) else _DEFAULT_STRIP_LEVEL
                    filename = number_to_filename.get(macro.number)
                    if filename is not None:
                        strip_levels[filename] = level
    except Exception:
        pass

    for filename in number_to_filename.values():
        strip_levels.setdefault(filename, _DEFAULT_STRIP_LEVEL)

    return strip_levels


class GetPackageInfoTool(Tool[GetPackageInfoToolInput, ToolRunOptions, GetPackageInfoToolOutput]):
    name = "get_package_info"
    description = """
    Extract package version, patch files, and patch strip levels from a spec file.

    Returns:
    - version: The package version (from Version: field)
    - patch_files: List of patch filenames in the order they appear (Patch0:, Patch1:, etc.)
    - patch_strip_levels: Mapping of each patch filename to its strip level (-p value)
      extracted from the %prep section (%autosetup, %autopatch, or individual %patch macros)

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
                    valid_patches = [p for p in patches if p.valid and p.expanded_location]
                    patch_files = [p.expanded_location for p in valid_patches]
                    number_to_filename = {p.number: p.expanded_location for p in valid_patches}

                strip_levels = _extract_strip_levels(spec, number_to_filename)

                return GetPackageInfoToolOutput(
                    result=PackageInfo(
                        version=version,
                        patch_files=patch_files,
                        patch_strip_levels=strip_levels,
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
    def _get_higher_stream_branch(dist_git_branch: str) -> str | None:
        if not (
            m := re.match(
                r"^(?P<prefix>rhel-(?P<x>\d+)\.)(?P<y>\d+)(?P<suffix>\.\d+)?$",
                dist_git_branch,
            )
        ):
            # not a Z-Stream branch
            return None
        y = int(m.group("y"))
        suffix = m.group("suffix") or ""
        return m.group("prefix") + str(min(y + 1, 10)) + suffix

    @staticmethod
    async def _get_latest_candidate_build(package: str, branch: str) -> EVR:
        candidate_tags = {branch + "-candidate", branch + "-z-candidate"}

        def get_latest_build(tag):
            builds = koji.ClientSession(BREWHUB_URL).listTagged(
                package=package,
                tag=tag,
                latest=True,
                inherit=True,
                strict=False,
            )
            if not builds:
                return None
            [build] = builds
            return EVR(
                epoch=build["epoch"] or 0,
                version=build["version"],
                release=build["release"],
            )

        results = await asyncio.gather(
            *(asyncio.to_thread(get_latest_build, tag) for tag in candidate_tags),
        )
        latest: EVR | None = None
        for result in results:
            if result is not None and (latest is None or latest < result):
                latest = result
        if latest is None:
            raise RuntimeError(f"There are no builds of {package} corresponding to {branch}")
        return latest

    @staticmethod
    def _find_macro(name: str, nodes: list[Node]) -> int | None:
        for index, node in reversed(list(enumerate(nodes))):
            if isinstance(node, (MacroSubstitution, EnclosedMacroSubstitution)) and node.name == name:
                return index
        return None

    @classmethod
    async def _bump_or_reset_release(cls, spec_path: Path, rebase: bool) -> None:
        with Specfile(spec_path) as spec:
            current_release = spec.raw_release
        nodes = ValueParser.parse(current_release)

        autorelease_index = cls._find_macro("autorelease", nodes)
        dist_index = cls._find_macro("dist", nodes)
        if autorelease_index is not None:
            # revert to plain %autorelease
            release = "%autorelease"
        else:
            if dist_index is None:
                prefix = current_release
                suffix = ""
            else:
                prefix = "".join(str(n) for n in nodes[:dist_index])
                suffix = "".join(str(n) for n in nodes[dist_index + 1 :])
            if m := re.match(r"^(\d+)(.*)$", prefix):
                # increase or reset the main numeric part
                release = str(1 if rebase else int(m.group(1)) + 1) + m.group(2)
            else:
                release = prefix + ".1"
            release += "%{?dist}"
            if not re.match(r"^\.\d+$", suffix):
                release += suffix

        with Specfile(spec_path) as spec:
            spec.raw_release = release

    @classmethod
    async def _set_zstream_release(
        cls,
        spec_path: Path,
        package: str,
        rebase: bool,
        current_stream_branch: str,
        higher_stream_branch: str,
    ) -> None:
        latest_current_stream_build, latest_higher_stream_build = await asyncio.gather(
            cls._get_latest_candidate_build(package, current_stream_branch),
            cls._get_latest_candidate_build(package, higher_stream_branch),
        )
        base_build = (
            latest_current_stream_build
            if EVR(epoch=latest_current_stream_build.epoch, version=latest_current_stream_build.version)
            < EVR(epoch=latest_higher_stream_build.epoch, version=latest_higher_stream_build.version)
            else latest_higher_stream_build
        )
        base_release, _ = base_build.release.rsplit(".el", maxsplit=1)
        with Specfile(spec_path) as spec:
            current_release = spec.raw_release
        nodes = ValueParser.parse(current_release)

        autorelease_index = cls._find_macro("autorelease", nodes)
        dist_index = cls._find_macro("dist", nodes)
        if autorelease_index is not None:
            if rebase:
                # %autorelease present, rebase, reset the release
                release = "0%{?dist}.%{autorelease -n}"
            elif dist_index is not None and autorelease_index > dist_index:
                # %autorelease after %dist, most likely already a Z-Stream release, no change needed
                release = current_release
            else:
                # no %dist or %autorelease before it, let's create a new release
                release = base_release + "%{?dist}.%{autorelease -n}"
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
                    # invalid Z-Stream counter, let's try to create a new release
                    release = base_release + "%{?dist}.1"
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
            if not (higher_stream_branch := self._get_higher_stream_branch(tool_input.dist_git_branch)):
                await self._bump_or_reset_release(spec_path, tool_input.rebase)
            else:
                await self._set_zstream_release(
                    spec_path,
                    tool_input.package,
                    tool_input.rebase,
                    tool_input.dist_git_branch,
                    higher_stream_branch,
                )
        except Exception as e:
            raise ToolError(f"Failed to update release: {e}") from e
        return StringToolOutput(result=f"Successfully updated release in {spec_path}")
