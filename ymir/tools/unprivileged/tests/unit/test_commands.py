import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware

from ymir.tools.unprivileged.commands import (
    RunShellCommandTool,
    RunShellCommandToolInput,
)


@pytest.mark.parametrize(
    "command, exit_code, stdout, stderr",
    [
        (
            "exit 28",
            28,
            None,
            None,
        ),
        (
            "echo -n test",
            0,
            "test",
            None,
        ),
        (
            "echo -n error >&2 && false",
            1,
            None,
            "error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_shell_command(command, exit_code, stdout, stderr):
    tool = RunShellCommandTool()
    output = await tool.run(input=RunShellCommandToolInput(command=command)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.to_json_safe()
    assert result.exit_code == exit_code
    assert result.stdout == stdout
    assert result.stderr == stderr


@pytest.mark.parametrize(
    "full_output",
    [False, True],
)
@pytest.mark.asyncio
async def test_run_shell_command_huge_output(full_output):
    command = "printf 'Line\n%.0s' {1..1000}"
    tool = RunShellCommandTool()
    output = await tool.run(
        input=RunShellCommandToolInput(command=command, full_output=full_output)
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.to_json_safe()
    assert result.exit_code == 0
    assert result.stderr is None
    if full_output:
        assert len(result.stdout.splitlines()) == 1000
        assert "[...]" not in result.stdout.splitlines()
    else:
        assert len(result.stdout.splitlines()) == 200
        assert "[...]" in result.stdout.splitlines()
