import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions

from utils import get_absolute_path


class GetCWDToolInput(BaseModel):
    pass


class GetCWDTool(Tool[GetCWDToolInput, ToolRunOptions, StringToolOutput]):
    name = "get_cwd"
    description = """
    Returns absolute path of the current working directory.
    """
    input_schema = GetCWDToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "filesystem", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: GetCWDToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        return StringToolOutput(result=str(get_absolute_path(Path("."), self)))


class RemoveToolInput(BaseModel):
    file: Path = Field(description="Path to a file to remove")


class RemoveTool(Tool[RemoveToolInput, ToolRunOptions, StringToolOutput]):
    name = "remove"
    description = """
    Removes the specified file.
    """
    input_schema = RemoveToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "filesystem", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: RemoveToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        file_path = get_absolute_path(tool_input.file, self)
        try:
            await asyncio.to_thread(file_path.unlink)
        except Exception as e:
            raise ToolError(f"Failed to remove file: {e}") from e
        return StringToolOutput(result=f"Successfully removed {file_path}")
