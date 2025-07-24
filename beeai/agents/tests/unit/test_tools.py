import pytest

from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware

from tools.shell_command import ShellCommandTool, ShellCommandToolInput


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
async def test_shell_command(command, exit_code, stdout, stderr):
    tool = ShellCommandTool()
    output = await tool.run(input=ShellCommandToolInput(command=command)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.to_json_safe()
    assert result.exit_code == exit_code
    assert result.stdout == stdout
    assert result.stderr == stderr
