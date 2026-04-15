import contextlib
from textwrap import dedent

import pytest

from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError

from ymir.tools.unprivileged.text import (
    CreateTool,
    CreateToolInput,
    InsertAfterSubstringTool,
    InsertAfterSubstringToolInput,
    ViewTool,
    ViewToolInput,
    InsertTool,
    InsertToolInput,
    StrReplaceTool,
    StrReplaceToolInput,
    SearchTextTool,
    SearchTextToolInput,
)


@pytest.mark.asyncio
async def test_create(tmp_path):
    test_file = tmp_path / "test.txt"
    content = "Line 1\nLine 2\n"
    tool = CreateTool()
    output = await tool.run(input=CreateToolInput(file=test_file, content=content)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result.startswith("Successfully")
    assert test_file.read_text() == content


@pytest.mark.asyncio
async def test_view(tmp_path):
    test_dir = tmp_path
    test_file = test_dir / "test.txt"
    content = "Line 1\nLine 2\nLine 3\n"
    test_file.write_text(content)
    tool = ViewTool()
    output = await tool.run(input=ViewToolInput(path=test_dir)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result == "test.txt\n"
    output = await tool.run(input=ViewToolInput(path=test_file)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result == content
    output = await tool.run(input=ViewToolInput(path=test_file, offset=1)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert (
        result
        == dedent(
            """
            Line 2
            Line 3
            """
        )[1:]
    )
    output = await tool.run(input=ViewToolInput(path=test_file, offset=1, limit=1)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert (
        result
        == dedent(
            """
            Line 2
            """
        )[1:]
    )


@pytest.mark.parametrize(
    "line, content",
    [
        (
            0,
            dedent(
                """
                Inserted line
                Line 1
                Line 2
                Line 3
                """
            )[1:],
        ),
        (
            1,
            dedent(
                """
                Line 1
                Inserted line
                Line 2
                Line 3
                """
            )[1:],
        ),
    ],
)
@pytest.mark.asyncio
async def test_insert(line, content, tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = InsertTool()
    output = await tool.run(
        input=InsertToolInput(file=test_file, line=line, new_string="Inserted line")
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    assert test_file.read_text() == content


@pytest.mark.parametrize(
    "insert_after_substring, final_content",
    [
        (
            "Line 1",
            "Line 1\nInserted line\nLine 2\nLine 3\n",
        ),
        (
            "Line 2",
            "Line 1\nLine 2\nInserted line\nLine 3\n",
        ),
        (
            "Line 3",
            "Line 1\nLine 2\nLine 3\nInserted line\n",
        ),
    ],
)
@pytest.mark.asyncio
async def test_insert_after_substring(insert_after_substring, final_content, tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = InsertAfterSubstringTool()
    output = await tool.run(
        input=InsertAfterSubstringToolInput(file=test_file, insert_after_substring=insert_after_substring, new_string="Inserted line")
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    assert test_file.read_text() == final_content


@pytest.mark.asyncio
async def test_insert_after_substring_missing(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = InsertAfterSubstringTool()
    with pytest.raises(ToolError) as e:
        await tool.run(
            input=InsertAfterSubstringToolInput(file=test_file, insert_after_substring="Line 4", new_string="Inserted line")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = e.value.message
    assert "No insertion was done because the specified substring wasn't present" in result


@pytest.mark.asyncio
async def test_str_replace(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = StrReplaceTool()
    output = await tool.run(
        input=StrReplaceToolInput(file=test_file, old_string="Line 2", new_string="LINE_2")
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    assert (
        test_file.read_text()
        == dedent(
            """
            Line 1
            LINE_2
            Line 3
            """
        )[1:]
    )


@pytest.mark.parametrize(
    "pattern, expected_output",
    [
        (
            "^Line",
            "1:Line 1\n2:Line 2\n3:Line 3\n",
        ),
        (
            "2$",
            "2:Line 2\n",
        ),
        (
            "Line 3",
            "3:Line 3\n",
        ),
        (
            "somethingelse",
            None,
        ),
    ],
)
@pytest.mark.asyncio
async def test_search_text(tmp_path, pattern, expected_output):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = SearchTextTool()
    with (pytest.raises(ToolError) if expected_output is None else contextlib.nullcontext()) as e:
        output = await tool.run(
            input=SearchTextToolInput(file=test_file, pattern=pattern)
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    if expected_output is not None:
        result = output.result
        assert result == expected_output
    else:
        assert e.value.message.endswith("No matches found")
