import asyncio
from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions


class ShellCommandToolInput(BaseModel):
    command: str = Field(description="Command to run.")


class ShellCommandToolResult(BaseModel):
    exit_code: int
    stdout: str | None
    stderr: str | None


class ShellCommandToolOutput(JSONToolOutput[ShellCommandToolResult]):
    pass


class ShellCommandTool(Tool[ShellCommandToolInput, ToolRunOptions, ShellCommandToolOutput]):
    name = "ShellCommand"
    description = """Runs commands in a shell."""
    input_schema = ShellCommandToolInput

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "shell", "command"],
            creator=self,
        )

    async def _run(
        self, tool_input: ShellCommandToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> ShellCommandToolOutput:
        proc = await asyncio.create_subprocess_shell(
            tool_input.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        result = {
            "exit_code": proc.returncode,
            "stdout": stdout.decode() if stdout else None,
            "stderr": stderr.decode() if stderr else None,
        }

        return ShellCommandToolOutput(ShellCommandToolResult.model_validate(result))
