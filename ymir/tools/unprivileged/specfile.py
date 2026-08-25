import asyncio
import logging
import re
from pathlib import Path

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

from ymir.common.utils import (
    NoBuildFoundError,
    get_absolute_path,
    get_all_patches,
    get_latest_candidate_build,
)
from ymir.common.version_utils import get_maintenance_rhel_branch
from ymir.tools.base import CloneableTool as Tool

logger = logging.getLogger(__name__)


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
        logger.debug("Failed to parse spec prep macros", exc_info=True)

    for filename in number_to_filename.values():
        strip_levels.setdefault(filename, _DEFAULT_STRIP_LEVEL)

    return strip_levels


class GetPackageInfoTool(Tool[GetPackageInfoToolInput, ToolRunOptions, GetPackageInfoToolOutput]):
    name = "get_package_info"
    timeout = 30
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
                valid_patches = [p for p in get_all_patches(spec) if p.valid and p.location]
                patch_files = [p.location for p in valid_patches]
                number_to_filename = {p.number: p.location for p in valid_patches}

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
    timeout = 30
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
                if spec.has_autochangelog:
                    return StringToolOutput(
                        result=f"Spec file {spec_path} uses %autochangelog. "
                        "Changelog is generated automatically, no manual entry needed. "
                        "Do not modify the changelog file."
                    )
                spec.add_changelog_entry(tool_input.content)
        except Exception as e:
            raise ToolError(f"Failed to add changelog entry: {e}") from e
        return StringToolOutput(result=f"Successfully added a new changelog entry to {spec_path}")


class UpdateReleaseToolInput(BaseModel):
    spec: Path = Field(description="Path to a spec file")
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="dist-git branch")
    rebase: bool = Field(description="Whether the Release update is done as part of a rebase")
    abandon_autorelease: bool = Field(
        default=False,
        description="If True, remove %autorelease from Z-stream releases and use a numeric counter instead",
    )
    treat_maintenance_rhel_as_zstream: bool = Field(
        default=False,
        description="If True, CentOS Stream branches for a RHEL version in maintenance phase (e.g. c8s) "
        "get Z-Stream release bumping against the internal RHEL branch. If False (default), such "
        "branches get a plain Y-Stream bump instead.",
    )
    disregard_zstream_nvr_policy: bool = Field(
        default=False,
        description="If True, a plain Y-Stream bump is used even for Z-Stream branches and "
        "maintenance-phase RHEL CentOS Stream branches, instead of the strict Z-Stream NVR "
        "ordering policy.",
    )


class UpdateReleaseTool(Tool[UpdateReleaseToolInput, ToolRunOptions, StringToolOutput]):
    name = "update_release"
    timeout = 30
    description = """
    Updates the value of the `Release` field in the specified spec file.

    If branch is a Z-Stream branch (rhel-X.Y or rhel-X.Y.Z), or a CentOS Stream branch for
    a RHEL version in maintenance phase (e.g. c8s) with treat_maintenance_rhel_as_zstream set
    to True, and disregard_zstream_nvr_policy is False, release is updated in the following way:
        - base release is established - from the latest candidate build of the current stream
          (for CentOS Stream branches corresponding to a RHEL version in maintenance phase,
          the internal RHEL branch is used for the candidate build lookup), unless the latest
          higher stream (Y + 1) candidate build shares the same version but has a higher release
          (not applicable to maintenance phase RHEL as there is no higher stream); if there is no
          candidate build for the higher stream, the current stream build is used as base
        - if there is no candidate build for the current stream, base release falls back to 0 if
          %autorelease is present in the current Release (it has no base release of its own to
          fall back to), otherwise to the numeric prefix of the release already present in the
          spec file (or 0 if there is none); the abandon_autorelease Z-stream counter falls back
          to 0 as well
        - if %autorelease is present in the current Release:
            - if abandon_autorelease is True, %autorelease is removed and Release is set to
              "N%{?dist}.1" (or "0%{?dist}.1" for rebase), using a plain numeric Z-stream counter
            - if %autorelease is after the dist tag, nothing is changed
            - otherwise, Release is set to "N%{?dist}.%{autorelease -n}", where N is base release
              or 0 in case of rebase
        - if there is no %autorelease:
            - in case of rebase, Release is set to "0%{?dist}.1"
            - if the dist tag in the current Release is followed by "." and a number, the number is increased
            - otherwise, ".1" is appended, unless there is a non-numeric suffix after the dist tag,
              in which case Release is set to "N%{?dist}.1", where N is base release

    Otherwise (branch is not a Z-Stream branch, or is a CentOS Stream branch for a
    maintenance-phase RHEL version with treat_maintenance_rhel_as_zstream set to False, or
    disregard_zstream_nvr_policy is True), release is updated in the following way:
        - if %autorelease is present in the current Release in any form, Release is set to plain %autorelease
        - otherwise, initial numeric part of Release is increased by one or reset to 1 in case of rebase
        - if there is no numeric part, ".1" is appended to whatever is before the dist tag
        - if the dist tag is followed by "." and a number, such suffix is removed
        - if there is no dist tag, it is added

    For Z-Stream, the goal is to ensure EVR of the build resulting from this spec file is higher than EVR
    of the latest current stream candidate build and at the same time lower than EVR of (potential) future
    higher stream candidate build.
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
    def _find_macro(name: str, nodes: list[Node]) -> int | None:
        for index, node in reversed(list(enumerate(nodes))):
            if isinstance(node, (MacroSubstitution, EnclosedMacroSubstitution)) and node.name == name:
                return index
        return None

    @staticmethod
    def _split_release_at_dist(
        current_release: str,
        expanded_raw_release: str,
        dist: str,
        nodes: list[Node],
        dist_index: int | None,
    ) -> tuple[str, str]:
        """Split a raw Release value into (prefix, suffix) around the %dist macro.

        Falls back to splitting the expanded Release on the expanded dist string when %dist
        isn't a directly parseable macro (e.g. it's embedded in a custom macro).
        """
        if dist_index is not None:
            return (
                "".join(str(n) for n in nodes[:dist_index]),
                "".join(str(n) for n in nodes[dist_index + 1 :]),
            )
        if dist and expanded_raw_release and dist in expanded_raw_release:
            prefix, suffix = expanded_raw_release.split(dist, 1)
            return prefix, suffix
        return current_release, ""

    @classmethod
    async def _bump_or_reset_release(cls, spec_path: Path, rebase: bool) -> None:
        with Specfile(spec_path) as spec:
            current_release = spec.raw_release
            expanded_raw_release = spec.expanded_raw_release
            dist = spec.expand("%{?dist}")
        nodes = ValueParser.parse(current_release)

        autorelease_index = cls._find_macro("autorelease", nodes)
        dist_index = cls._find_macro("dist", nodes)
        if autorelease_index is not None:
            # revert to plain %autorelease
            release = "%autorelease"
        else:
            prefix, suffix = cls._split_release_at_dist(
                current_release, expanded_raw_release, dist, nodes, dist_index
            )
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

    @staticmethod
    def _extract_release_without_dist(evr: EVR) -> str:
        return evr.release.rsplit(".el", maxsplit=1)[0]

    @staticmethod
    def _extract_zstream_suffix(evr: EVR) -> int:
        parts = evr.release.rsplit(".el", maxsplit=1)
        if len(parts) < 2:
            return 0
        after_el = parts[1]
        dot_parts = after_el.split(".", 1)
        if len(dot_parts) > 1:
            try:
                return int(dot_parts[1])
            except ValueError:
                return 0
        return 0

    @classmethod
    async def _resolve_zstream_base_build(
        cls,
        package: str,
        current_stream_branch: str,
        higher_stream_branch: str | None,
    ) -> tuple[EVR | None, EVR | None]:
        """Determine which build's release the new Z-Stream release should be based on.

        Returns (base_build, latest_current_stream_build): the latter is also returned on its own
        because it (not necessarily base_build, which may be the higher stream's build) is what
        the Z-Stream counter is incremented from. Either may be None if there is simply no
        candidate build yet for the respective branch; any other error looking up a build is
        raised instead of being treated as "no build".
        """
        if not higher_stream_branch:
            try:
                latest_current_stream_build, _ = await get_latest_candidate_build(
                    package, current_stream_branch
                )
            except NoBuildFoundError:
                return None, None
            return latest_current_stream_build, latest_current_stream_build

        current_task = asyncio.ensure_future(get_latest_candidate_build(package, current_stream_branch))
        higher_task = asyncio.ensure_future(get_latest_candidate_build(package, higher_stream_branch))
        tasks = (current_task, higher_task)
        try:
            # return as soon as either lookup raises, rather than always waiting for both, so a
            # real failure (as opposed to NoBuildFoundError) doesn't sit blocked on a slow Koji
            # call
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            fatal_error = None
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, NoBuildFoundError):
                    fatal_error = exc
                    break
            if fatal_error is not None:
                raise fatal_error
            if pending:
                await asyncio.wait(pending)
            for task in tasks:
                exc = task.exception()
                if exc is not None and not isinstance(exc, NoBuildFoundError):
                    raise exc
        except BaseException:
            # make sure neither lookup outlives this call - on a fatal error or on this coroutine
            # itself being cancelled, cancel whatever is still running and wait for it to actually
            # finish before propagating
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        try:
            latest_current_stream_build, _ = current_task.result()
        except NoBuildFoundError:
            return None, None

        try:
            latest_higher_stream_build, _ = higher_task.result()
        except NoBuildFoundError:
            # no higher stream build to compare against (yet), just use the current stream one
            return latest_current_stream_build, latest_current_stream_build
        higher_stream_takes_over = EVR(
            epoch=latest_higher_stream_build.epoch, version=latest_higher_stream_build.version
        ) == EVR(
            epoch=latest_current_stream_build.epoch, version=latest_current_stream_build.version
        ) and EVR(version="0", release=cls._extract_release_without_dist(latest_higher_stream_build)) >= EVR(
            version="0", release=cls._extract_release_without_dist(latest_current_stream_build)
        )
        base_build = latest_higher_stream_build if higher_stream_takes_over else latest_current_stream_build
        return base_build, latest_current_stream_build

    @classmethod
    async def _set_zstream_release(
        cls,
        spec_path: Path,
        package: str,
        rebase: bool,
        current_stream_branch: str,
        higher_stream_branch: str | None = None,
        abandon_autorelease: bool = False,
    ) -> None:
        with Specfile(spec_path) as spec:
            current_release = spec.raw_release
            expanded_raw_release = spec.expanded_raw_release
            dist = spec.expand("%{?dist}")
        nodes = ValueParser.parse(current_release)

        autorelease_index = cls._find_macro("autorelease", nodes)
        dist_index = cls._find_macro("dist", nodes)

        base_build, latest_current_stream_build = await cls._resolve_zstream_base_build(
            package, current_stream_branch, higher_stream_branch
        )
        if base_build is not None:
            base_release = cls._extract_release_without_dist(base_build)
        elif autorelease_index is not None:
            # no candidate build yet and %autorelease hasn't been given an established Z-stream
            # base release of its own - expanding it would only yield its own commit-count
            # counter (unrelated to the base release we need here), so start fresh like a rebase
            base_release = "0"
        else:
            # no candidate build yet for the current stream: fall back to the release already
            # present in the spec file
            prefix, _ = cls._split_release_at_dist(
                current_release, expanded_raw_release, dist, nodes, dist_index
            )
            match = re.match(r"^(\d+(?:\.\d+)*)", prefix)
            base_release = match.group(1) if match else "0"

        if autorelease_index is not None:
            if abandon_autorelease:
                zstream_suffix = (
                    cls._extract_zstream_suffix(latest_current_stream_build)
                    if latest_current_stream_build
                    else 0
                )
                release = f"{'0' if rebase else base_release}%{{?dist}}.{1 if rebase else zstream_suffix + 1}"
            elif rebase:
                # %autorelease present, rebase, reset the release
                release = "0%{?dist}.%{autorelease -n}"
            elif dist_index is not None and autorelease_index > dist_index:
                # %autorelease after %dist, most likely already a Z-Stream release, no change needed
                release = current_release
            else:
                # no %dist or %autorelease before it, let's create a new release
                release = f"{base_release}%{{?dist}}.%{{autorelease -n}}"
        else:
            if rebase:
                # no %autorelease, rebase, reset the release
                release = "0%{?dist}.1"
            elif dist_index is None:
                # no %autorelease and no %dist
                prefix, suffix = cls._split_release_at_dist(
                    current_release, expanded_raw_release, dist, nodes, dist_index
                )
                if m := re.match(r"^\.(\d+)$", suffix):
                    # %dist is embedded in a macro, use the expanded form
                    release = f"{prefix}%{{?dist}}.{int(m.group(1)) + 1}"
                else:
                    release = prefix + "%{?dist}.1"
            elif dist_index + 1 < len(nodes):
                prefix = "".join(str(n) for n in nodes[: dist_index + 1])
                suffix = "".join(str(n) for n in nodes[dist_index + 1 :])
                if m := re.match(r"^\.(\d+)$", suffix):
                    # no %autorelease and existing Z-Stream counter after %dist, increase it
                    release = prefix + "." + str(int(m.group(1)) + 1)
                else:
                    # invalid Z-Stream counter, let's try to create a new release
                    release = f"{base_release}%{{?dist}}.1"
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
            if tool_input.disregard_zstream_nvr_policy:
                await self._bump_or_reset_release(spec_path, tool_input.rebase)
            else:
                higher_stream_branch = self._get_higher_stream_branch(tool_input.dist_git_branch)
                maintenance_rhel_branch = (
                    not higher_stream_branch
                    and tool_input.treat_maintenance_rhel_as_zstream
                    and await get_maintenance_rhel_branch(tool_input.dist_git_branch)
                )
                if higher_stream_branch or maintenance_rhel_branch:
                    await self._set_zstream_release(
                        spec_path,
                        tool_input.package,
                        tool_input.rebase,
                        maintenance_rhel_branch or tool_input.dist_git_branch,
                        higher_stream_branch,
                        abandon_autorelease=tool_input.abandon_autorelease,
                    )
                else:
                    await self._bump_or_reset_release(spec_path, tool_input.rebase)
        except Exception as e:
            raise ToolError(f"Failed to update release: {e}") from e
        return StringToolOutput(result=f"Successfully updated release in {spec_path}")
