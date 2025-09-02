from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from specfile import Specfile
from specfile.utils import EVR

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions

from common.validators import AbsolutePath, NonEmptyString


# version update & release reset ?
# patch addition ?


class AddChangelogEntryToolInput(BaseModel):
    spec: AbsolutePath = Field(description="Absolute path to a spec file")
    content: list[str] = Field(
        description="""
        Content of the entry as a list of lines, maximum line length should be 80 characters,
        every paragraph should start with "- "
        """
    )
    author: NonEmptyString | None = Field(description="Author of the entry (change)", default=None)
    email: NonEmptyString | None = Field(description="E-mail address of the author", default=None)


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
        self, tool_input: AddChangelogEntryToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            with Specfile(tool_input.spec) as spec:
                spec.add_changelog_entry(tool_input.content, author=tool_input.author, email=tool_input.email)
        except Exception as e:
            return StringToolOutput(result=f"Failed to add changelog entry: {e}")
        return StringToolOutput(result=f"Successfully added a new changelog entry to {tool_input.spec}")


class BumpReleaseToolInput(BaseModel):
    spec: AbsolutePath = Field(description="Absolute path to a spec file")


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
        try:
            with Specfile(tool_input.spec) as spec:
                spec.bump_release()
        except Exception as e:
            return StringToolOutput(result=f"Failed to bump release: {e}")
        return StringToolOutput(result=f"Successfully bumped release in {tool_input.spec}")


class SetZStreamReleaseToolInput(BaseModel):
    spec: AbsolutePath = Field(description="Absolute path to a spec file")
    latest_ystream_evr: NonEmptyString = Field(description="EVR (Epoch-Version-Release) of the latest Y-Stream build")


class SetZStreamReleaseTool(Tool[SetZStreamReleaseToolInput, ToolRunOptions, StringToolOutput]):
    name = "set_zstream_release"
    description = """
    Sets the value of the `Release` field in the specified spec file to a Z-Stream release
    based on the release of the latest Y-Stream build.
    """
    input_schema = SetZStreamReleaseToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "specfile", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: SetZStreamReleaseToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            with Specfile(tool_input.spec) as spec:
                if not spec.has_autorelease:
                    return StringToolOutput(result="The specified spec file doesn't use %autorelease")
                base_release = EVR.from_string(tool_input.latest_ystream_evr).release
                base_raw_release = base_release.rsplit(".el", maxsplit=1)[0]
                spec.raw_release = base_raw_release + "%{?dist}.%{autorelease -n}"
        except Exception as e:
            return StringToolOutput(result=f"Failed to set Z-Stream release: {e}")
        return StringToolOutput(result=f"Successfully set Z-Stream release in {tool_input.spec}")
