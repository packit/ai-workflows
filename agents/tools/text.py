import asyncio
import re
from pathlib import Path

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions

from common.validators import NonEmptyString
from utils import get_absolute_path


class CreateToolInput(BaseModel):
    file: Path = Field(description="Path to a file to create")
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
        file_path = get_absolute_path(tool_input.file, self)
        try:
            await asyncio.to_thread(file_path.write_text, tool_input.content)
        except Exception as e:
            raise ToolError(f"Failed to create file: {e}") from e
        return StringToolOutput(result=f"Successfully created {file_path} with the specified text")


class ViewToolInput(BaseModel):
    path: Path = Field(description="Path to a file or directory to view")
    offset: int | None = Field(
        description="For text files only: 0-based line number to start viewing from",
        ge=0,
        default=None,
    )
    limit: int | None = Field(
        description=(
            "For text files only: Maximum number of lines to view (default: 1000). "
            "For files longer than 1000 lines, use with `offset` to paginate: offset=0 for lines 0-999, offset=1000 for lines 1000-1999, etc."
        ),
        gt=0,
        default=1000,
        le=1000,
    )


class ViewTool(Tool[ViewToolInput, ToolRunOptions, StringToolOutput]):
    name = "view"
    description = """
    Outputs the contents of a file or lists the contents of a directory.
    For text files, returns up to 1000 lines. Use `offset` and `limit` to paginate large text files.
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
        path = get_absolute_path(tool_input.path, self)
        try:
            if path.is_file():
                content = await asyncio.to_thread(path.read_text)
                lines = content.splitlines(keepends=True)
                start = tool_input.offset
                end = None
                if tool_input.limit is not None:
                    end = (start or 0) + tool_input.limit
                return StringToolOutput(result="".join(lines[start:end]))
            return StringToolOutput(result="\n".join(e.name for e in path.iterdir()) + "\n")
        except Exception as e:
            raise ToolError(f"Failed to view path: {e}") from e


class InsertToolInput(BaseModel):
    file: Path = Field(description="Path to a file to edit")
    line: int = Field(description="Line number after which to insert the text (0 for beginning of file)")
    new_string: str = Field(description="Text to insert")


class InsertTool(Tool[InsertToolInput, ToolRunOptions, StringToolOutput]):
    name = "insert"
    description = "Inserts the specified text at a specific location in a file."
    input_schema = InsertToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "text", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: InsertToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        file_path = get_absolute_path(tool_input.file, self)
        try:
            lines = (await asyncio.to_thread(file_path.read_text)).splitlines(keepends=True)
            lines.insert(tool_input.line, tool_input.new_string + "\n")
            await asyncio.to_thread(file_path.write_text, "".join(lines))
        except Exception as e:
            raise ToolError(f"Failed to insert text: {e}") from e
        return StringToolOutput(result=f"Successfully inserted the specified text into {file_path}")


class InsertAfterSubstringToolInput(BaseModel):
    file: Path = Field(description="Path to a file to edit")
    insert_after_substring: NonEmptyString = Field(description="Substring to insert the text after")
    new_string: NonEmptyString = Field(description="Text to insert")


class InsertAfterSubstringTool(Tool[InsertAfterSubstringToolInput, ToolRunOptions, StringToolOutput]):
    name = "insert_after_substring"
    description = """
    Inserts the provided text `new_string` on a new line after the first
    occurrence of the specified substring `insert_after_substring`. The insertion
    happens only once.
    """
    input_schema = InsertAfterSubstringToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "text", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: InsertAfterSubstringToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        file_path = get_absolute_path(tool_input.file, self)
        try:
            content = await asyncio.to_thread(file_path.read_text)
            if tool_input.insert_after_substring not in content:
                raise ToolError("No insertion was done because the specified substring wasn't present")
            await asyncio.to_thread(
                file_path.write_text,
                content.replace(
                    tool_input.insert_after_substring,
                    tool_input.insert_after_substring + "\n" + tool_input.new_string,
                    1  # Replace only the first occurrence, 'count' kw introduced in Python 3.13
                )
            )
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to insert text: {e}") from e
        return StringToolOutput(result=f"Successfully inserted the specified text into {file_path}")


class StrReplaceToolInput(BaseModel):
    file: Path = Field(description="Path to a file to edit")
    old_string: str = Field(description="The exact literal text to replace")
    new_string: str = Field(description="New text to insert in place of the old text")


class StrReplaceTool(Tool[StrReplaceToolInput, ToolRunOptions, StringToolOutput]):
    name = "str_replace"
    description = """
    Replaces a specific string in the specified file with a new string.

    CRITICAL: The `old_string` argument must uniquely identify the single instance to change.
    It should include at least 3 lines of context before and after the target text,
    matching whitespace and indentation precisely.
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
        file_path = get_absolute_path(tool_input.file, self)
        try:
            content = await asyncio.to_thread(file_path.read_text)
            if (count := content.count(tool_input.old_string)) == 0:
                raise ToolError("No replacement was done because the specified text to replace wasn't present")
            elif count > 1:
                raise ToolError(
                    "No replacement was done because the specified text is not unique."
                    "Please provide more context!"
                )
            await asyncio.to_thread(
                file_path.write_text, content.replace(tool_input.old_string, tool_input.new_string)
            )
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to replace text: {e}") from e
        return StringToolOutput(result=f"Successfully replaced the specified text in {file_path}")


class SearchTextToolInput(BaseModel):
    file: Path = Field(description="Path to a file to search in")
    pattern: str = Field(description="Regular expression pattern to search for")


class SearchTextTool(Tool[SearchTextToolInput, ToolRunOptions, StringToolOutput]):
    name = "search_text"
    description = """
    Search for a specific regex pattern in the specified file. Returns lines matching the pattern
    along with their line numbers. Line numbers are 1-indexed.
    """
    input_schema = SearchTextToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "text", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: SearchTextToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        file_path = get_absolute_path(tool_input.file, self)
        try:
            pattern = re.compile(tool_input.pattern)
            content = await asyncio.to_thread(file_path.read_text)
            result = [
                f"{num + 1}:{line}"
                for num, line in enumerate(content.splitlines(keepends=True))
                if pattern.search(line)
            ]
            if not result:
                raise ToolError("No matches found")
            return StringToolOutput(result="".join(result))
        except re.error as e:
            raise ToolError(f"Invalid regular expression: {e}") from e
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to search text: {e}") from e
