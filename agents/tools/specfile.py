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
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions

from common.validators import NonEmptyString
from constants import BREWHUB_URL
from utils import get_absolute_path


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


class BumpReleaseToolInput(BaseModel):
    spec: Path = Field(description="Path to a spec file")


class BumpReleaseTool(Tool[BumpReleaseToolInput, ToolRunOptions, StringToolOutput]):
    name = "bump_release"
    description = """
    Bumps (increments) the value of `Release` in the specified spec file.
    """
    input_schema = BumpReleaseToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "specfile", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: BumpReleaseToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        spec_path = get_absolute_path(tool_input.spec, self)
        try:
            with Specfile(spec_path) as spec:
                spec.bump_release()
        except Exception as e:
            raise ToolError(f"Failed to bump release: {e}") from e
        return StringToolOutput(result=f"Successfully bumped release in {spec_path}")


class SetZStreamReleaseToolInput(BaseModel):
    spec: Path = Field(description="Path to a spec file")
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="dist-git branch")
    rebase: bool = Field(description="Whether the Release update is done as part of a rebase")


class SetZStreamReleaseTool(Tool[SetZStreamReleaseToolInput, ToolRunOptions, StringToolOutput]):
    name = "set_zstream_release"
    description = """
    Sets the value of the `Release` field in the specified spec file to a Z-Stream release
    based on the current release or the release of the latest Y-Stream build.
    """
    input_schema = SetZStreamReleaseToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "specfile", self.name],
            creator=self,
        )

    async def _get_latest_ystream_build(self, package: str, dist_git_branch: str) -> EVR:
        if not (m := re.match(r"^(?P<prefix>rhel-\d+\.)(?P<y>\d+)(?P<suffix>\.\d+)?$", dist_git_branch)):
            raise ValueError(f"Unexpected dist-git branch: {dist_git_branch}")
        candidate_tag = (
            m.group("prefix")
            # y++, up to 10 (highest RHEL minor version)
            + str(min(int(m.group("y")) + 1, 10))
            + (m.group("suffix") or "")
            + "-candidate"
        )
        builds = await asyncio.to_thread(
            koji.ClientSession(BREWHUB_URL).listTagged,
            package=package,
            tag=candidate_tag,
            latest=True,
            inherit=True,
            strict=True,
        )
        if not builds:
            raise RuntimeError(f"There are no Y-Stream builds for {package} and {dist_git_branch}")
        [build] = builds
        return EVR(epoch=build["epoch"] or 0, version=build["version"], release=build["release"])

    async def _run(
        self,
        tool_input: SetZStreamReleaseToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        spec_path = get_absolute_path(tool_input.spec, self)
        try:
            latest_ystream_build = self._get_latest_ystream_build(
                tool_input.package,
                tool_input.dist_git_branch,
            )
            ystream_base_release, suffix = latest_ystream_build.release.rsplit(".el", maxsplit=1)
            ystream_release_suffix = f".el{suffix}"
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
                if tool_input.rebase:
                    # %autorelease present, rebase, reset the release
                    release = "0%{?dist}.%{autorelease -n}"
                elif dist_index is not None and autorelease_index > dist_index:
                    # %autorelease after %dist, most likely already a Z-Stream release, no change needed
                    release = current_release
                else:
                    # no %dist or %autorelease before it, let's create a new release based on Y-Stream
                    release = ystream_base_release + "%{?dist}.%{autorelease -n}"
            else:
                if tool_input.rebase:
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
                        # invalid Z-Stream counter, let's try to create a new release based on Y-Stream
                        release = ystream_base_release + "%{?dist}.1"
                else:
                    # no %autorelease, %dist present, add Z-Stream counter
                    release = current_release + ".1"

            def get_zstream_dist_tag():
                if not (m := re.match(r"^rhel-(\d+)\.(\d+)(\.\d+)?$", tool_input.dist_git_branch)):
                    raise ValueError(f"Unexpected dist-git branch: {tool_input.dist_git_branch}")
                return ".el" + m.group(1) + "_" + m.group(2)

            with Specfile(spec_path, macros=[("dist", get_zstream_dist_tag())]) as spec:
                current_evr = EVR(
                    version=latest_ystream_build.version,
                    release=spec.expand(
                        current_release, extra_macros=[("_rpmautospec_release_number", "1")]
                    ),
                )
                evr = EVR(
                    version=latest_ystream_build.version,
                    release=spec.expand(release, extra_macros=[("_rpmautospec_release_number", "2")]),
                )
                future_ystream_evr = EVR(
                    version=latest_ystream_build.version,
                    release = str(int(ystream_base_release) + 1) + ystream_release_suffix,
                )
                # sanity check
                if not tool_input.rebase and not (current_evr < evr < future_ystream_evr):
                    raise ToolError("Unable to determine valid release")
                spec.raw_release = release
        except Exception as e:
            raise ToolError(f"Failed to set Z-Stream release: {e}") from e
        return StringToolOutput(result=f"Successfully set Z-Stream release in {spec_path}")
