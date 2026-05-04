from pathlib import Path

import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError

from ymir.tools.unprivileged.filesystem import (
    GetCWDTool,
    GetCWDToolInput,
    RemoveTool,
    RemoveToolInput,
)


@pytest.mark.asyncio
async def test_get_cwd(tmp_path):
    tool = GetCWDTool(options={"working_directory": tmp_path})
    output = await tool.run(input=GetCWDToolInput()).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert Path(result) == tmp_path


@pytest.mark.asyncio
async def test_remove(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.touch()
    tool = RemoveTool()
    output = await tool.run(input=RemoveToolInput(file=test_file)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result.startswith("Successfully")
    assert not test_file.is_file()
    with pytest.raises(ToolError) as e:
        output = await tool.run(input=RemoveToolInput(file=test_file)).middleware(
            GlobalTrajectoryMiddleware(pretty=True)
        )
    assert e.value.message.startswith("Failed to remove file")
