import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions

from common.validators import AbsolutePath, Range


class CreateToolInput(BaseModel):
    file: AbsolutePath = Field(description="Absolute path to a file to create")
    content: str = Field(description="Content to write to the new file")


class CreateTool(Tool[CreateToolInput, ToolRunOptions, StringToolOutput]):
    name = "create"
    description = """
    Creates a new file with the specified content.
    """
    input_schema = CreateToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "text", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: CreateToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            await asyncio.to_thread(tool_input.file.write_text, tool_input.content)
        except Exception as e:
            raise ToolError(f"Failed to create file: {e}") from e
        return StringToolOutput(result=f"Successfully created {tool_input.file} with the specified text")


class ViewToolInput(BaseModel):
    path: AbsolutePath = Field(description="Absolute path to a file or directory to view")
    view_range: Range | None = Field(
        description="""
        List of two integers specifying the start and end line numbers to view.
        Line numbers are 1-indexed, and -1 for the end line means read to the end of the file.
        This argument only applies when viewing files, not directories.
        """,
        default=None,
    )


class ViewTool(Tool[ViewToolInput, ToolRunOptions, StringToolOutput]):
    name = "view"
    description = """
    Outputs the contents of a file or lists the contents of a directory. Can read an entire file
    or a specific range of lines.
    """
    input_schema = ViewToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "text", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: ViewToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            if tool_input.path.is_file():
                content = await asyncio.to_thread(tool_input.path.read_text)
                if tool_input.view_range is not None:
                    start, end = tool_input.view_range
                    lines = content.splitlines(keepends=True)
                    content = "".join(lines[start - 1 : None if end < 0 else end])
                return StringToolOutput(result=content)
            return StringToolOutput(result="\n".join(e.name for e in tool_input.path.iterdir()) + "\n")
        except Exception as e:
            raise ToolError(f"Failed to view path: {e}") from e


class InsertToolInput(BaseModel):
    file: AbsolutePath = Field(description="Absolute path to a file to edit")
    line: int = Field(description="Line number after which to insert the text (0 for beginning of file)")
    new_string: str = Field(description="Text to insert")


class InsertTool(Tool[InsertToolInput, ToolRunOptions, StringToolOutput]):
    name = "insert"
    description = """
    Inserts the specified text at a specific location in a file.
    """
    input_schema = InsertToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "text", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: InsertToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            lines = (await asyncio.to_thread(tool_input.file.read_text)).splitlines(keepends=True)
            lines.insert(tool_input.line, tool_input.new_string + "\n")
            await asyncio.to_thread(tool_input.file.write_text, "".join(lines))
        except Exception as e:
            raise ToolError(f"Failed to insert text: {e}") from e
        return StringToolOutput(result=f"Successfully inserted the specified text into {tool_input.file}")


class StrReplaceToolInput(BaseModel):
    file: AbsolutePath = Field(description="Absolute path to a file to edit")
    old_string: str = Field(
        description="Text to replace (must match exactly, including whitespace and indentation)"
    )
    new_string: str = Field(description="New text to insert in place of the old text")


class StrReplaceTool(Tool[StrReplaceToolInput, ToolRunOptions, StringToolOutput]):
    name = "str_replace"
    description = """
    Replaces a specific string in the specified file with a new string.
    """
    input_schema = StrReplaceToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "text", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: StrReplaceToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            content = await asyncio.to_thread(tool_input.file.read_text)
            if tool_input.old_string not in content:
                raise ToolError("No replacement was done because the specified text to replace wasn't present")
            await asyncio.to_thread(
                tool_input.file.write_text, content.replace(tool_input.old_string, tool_input.new_string)
            )
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to replace text: {e}") from e
        return StringToolOutput(result=f"Successfully replaced the specified text in {tool_input.file}")
