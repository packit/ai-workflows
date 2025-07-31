import asyncio
from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions


class RunShellCommandToolInput(BaseModel):
    command: str = Field(description="Command to run")


class RunShellCommandToolResult(BaseModel):
    exit_code: int
    stdout: str | None
    stderr: str | None


class RunShellCommandToolOutput(JSONToolOutput[RunShellCommandToolResult]):
    pass


class RunShellCommandTool(Tool[RunShellCommandToolInput, ToolRunOptions, RunShellCommandToolOutput]):
    name = "run_shell_command"
    description = """Runs the specified command in a shell."""
    input_schema = RunShellCommandToolInput

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "shell", "command"],
            creator=self,
        )

    async def _run(
        self, tool_input: RunShellCommandToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> RunShellCommandToolOutput:
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

        return RunShellCommandToolOutput(RunShellCommandToolResult.model_validate(result))
