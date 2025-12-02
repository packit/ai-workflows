import asyncio
import math
from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolError, ToolRunOptions

from agents.utils import run_subprocess

TIMEOUT = 10 * 60  # seconds
ELLIPSIZED_LINES = 200


class RunShellCommandToolInput(BaseModel):
    command: str = Field(description="Command to run")
    full_output: bool = Field(
        default=False,
        description=(
            "Whether the content of stdout and stderr should be included in full. "
            f"Only approximately {ELLIPSIZED_LINES // 2} lines from the beginning "
            "and the end are included by default."
        ),
    )


class RunShellCommandToolResult(BaseModel):
    exit_code: int
    stdout: str | None
    stderr: str | None


class RunShellCommandToolOutput(JSONToolOutput[RunShellCommandToolResult]):
    pass


class RunShellCommandTool(Tool[RunShellCommandToolInput, ToolRunOptions, RunShellCommandToolOutput]):
    name = "run_shell_command"
    description = """
        Runs the specified command in a shell. Returns a dictionary with exit code
        and captured stdout and stderr.
    """
    input_schema = RunShellCommandToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "commands", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: RunShellCommandToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> RunShellCommandToolOutput:
        try:
            exit_code, stdout, stderr = await asyncio.wait_for(
                run_subprocess(tool_input.command, shell=True, cwd=(self.options or {}).get("working_directory")),
                timeout=TIMEOUT,
            )
        except TimeoutError as e:
            raise ToolError(f"The specified command timed out after {TIMEOUT} seconds") from e

        def ellipsize(output):
            if output is None:
                return None
            if tool_input.full_output:
                return output
            lines = output.splitlines(keepends=True)
            if len(lines) <= ELLIPSIZED_LINES:
                return output
            return "".join(
                lines[: math.floor((ELLIPSIZED_LINES - 1) / 2)]
                + ["[...]\n"]
                + lines[-math.ceil((ELLIPSIZED_LINES - 1) / 2) :]
            )

        result = {
            "exit_code": exit_code,
            "stdout": ellipsize(stdout),
            "stderr": ellipsize(stderr),
        }
        return RunShellCommandToolOutput(RunShellCommandToolResult.model_validate(result))
