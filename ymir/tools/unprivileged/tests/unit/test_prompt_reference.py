from pathlib import Path

import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError

from ymir.agents.utils import get_reference_dir
from ymir.tools.unprivileged.prompt_reference import (
    LoadPromptInput,
    LoadPromptTool,
    split_frontmatter,
)


@pytest.fixture
def references_dir(tmp_path: Path) -> Path:
    (tmp_path / "alpha.md").write_text(
        "---\nname: alpha\ndescription: Use when testing alpha.\n---\n\n# Alpha\n\nBody A.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\nname: beta\ndescription: Use when testing beta.\n---\n\n# Beta\n\nBody B.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_catalog_lists_names_without_bodies(references_dir):
    tool = LoadPromptTool(references_dir)
    assert "alpha" in tool.description
    assert "Use when testing alpha." in tool.description
    assert "Body A." not in tool.description


def test_ignores_non_markdown_files(references_dir):
    (references_dir / "notes.txt").write_text("not a reference", encoding="utf-8")
    tool = LoadPromptTool(references_dir)
    assert "notes" not in tool.description


def test_split_frontmatter():
    meta, body = split_frontmatter("---\nname: x\ndescription: y\n---\n\nHello\n")
    assert meta == {"name": "x", "description": "y"}
    assert body == "Hello\n"


def test_split_frontmatter_without_header():
    meta, body = split_frontmatter("Just text\n")
    assert meta == {}
    assert body == "Just text\n"


def test_missing_directory_raises(tmp_path):
    with pytest.raises(ValueError):
        LoadPromptTool(tmp_path / "does-not-exist")


@pytest.mark.asyncio
async def test_loads_body_without_frontmatter(references_dir):
    tool = LoadPromptTool(references_dir)
    output = await tool.run(input=LoadPromptInput(name="alpha")).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result.startswith("# Alpha")
    assert "Body A." in result
    assert "name: alpha" not in result


@pytest.mark.asyncio
async def test_omitting_name_lists_references(references_dir):
    tool = LoadPromptTool(references_dir)
    output = await tool.run(input=LoadPromptInput()).middleware(GlobalTrajectoryMiddleware(pretty=True))
    assert "alpha" in output.result
    assert "beta" in output.result


@pytest.mark.asyncio
async def test_unknown_reference_raises(references_dir):
    tool = LoadPromptTool(references_dir)
    with pytest.raises(ToolError):
        await tool.run(input=LoadPromptInput(name="does-not-exist")).middleware(
            GlobalTrajectoryMiddleware(pretty=True)
        )


def test_triage_references_are_wired():
    """get_reference_dir() resolves the real triage references and validate_fix loads.

    Guards the silent failure mode: a wrong directory or extension would leave
    the tool with an empty catalog rather than raising.
    """
    tool = LoadPromptTool(get_reference_dir("triage"))
    assert "validate_fix" in tool.description
    assert "rebuild" in tool.description
