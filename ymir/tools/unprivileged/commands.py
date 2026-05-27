import asyncio
import json
import math
import os
import shlex

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.common.base_utils import run_subprocess
from ymir.tools.base import CloneableTool as Tool

TIMEOUT = 10 * 60  # seconds
ELLIPSIZED_LINES = 200
URL_FETCH_COMMANDS = ["curl", "wget"]


def _get_blocked_urls() -> list[str]:
    """Return the list of blocked URL prefixes from ``MOCK_BLOCKED_URLS``.

    The env var accepts either a JSON array or a comma-separated string.

    Returns:
        A list of URL prefix strings to block.
    """
    if not (raw := os.getenv("MOCK_BLOCKED_URLS", "")):
        return []
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.decoder.JSONDecodeError:
        return [stripped for url in raw.split(",") if (stripped := url.strip())]


def _check_blocked_urls(command: str, blocked_urls: list[str]) -> str | None:
    """Check whether a shell command targets a blocked URL via curl/wget.

    Args:
        command: The shell command string to inspect.
        blocked_urls: URL prefixes that should be blocked.

    Returns:
        The matched blocked URL prefix, or ``None`` if no match is found.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    suffixes = tuple(f"/{cmd}" for cmd in URL_FETCH_COMMANDS)
    has_url_fetch_cmd = any(tok in URL_FETCH_COMMANDS or tok.endswith(suffixes) for tok in tokens)
    if not has_url_fetch_cmd:
        return None

    for token in tokens:
        for blocked in blocked_urls:
            if token.startswith(blocked):
                return blocked
    return None


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
        self,
        tool_input: RunShellCommandToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> RunShellCommandToolOutput:
        blocked_urls = _get_blocked_urls()
        if blocked_urls:
            blocked = _check_blocked_urls(tool_input.command, blocked_urls)
            if blocked:
                raise ToolError(
                    f"BLOCKED: {blocked} is mocked locally; "
                    f"use git commands instead of {'/'.join(URL_FETCH_COMMANDS)}"
                )

        try:
            exit_code, stdout, stderr = await asyncio.wait_for(
                run_subprocess(  # noqa: S604
                    tool_input.command,
                    shell=True,
                    cwd=(self.options or {}).get("working_directory"),
                    env=(self.options or {}).get("env"),
                ),
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
                [
                    *lines[: math.floor((ELLIPSIZED_LINES - 1) / 2)],
                    "[...]\n",
                    *lines[-math.ceil((ELLIPSIZED_LINES - 1) / 2) :],
                ]
            )

        result = {
            "exit_code": exit_code,
            "stdout": ellipsize(stdout),
            "stderr": ellipsize(stderr),
        }
        return RunShellCommandToolOutput(RunShellCommandToolResult.model_validate(result))
