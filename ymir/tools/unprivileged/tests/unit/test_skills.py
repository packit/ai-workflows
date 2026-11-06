from pathlib import Path

import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError

from ymir.agents.utils import get_skills_dir
from ymir.tools.unprivileged.skills import LoadSkillInput, LoadSkillTool, split_frontmatter


def _write_skill(
    root: Path, name: str, description: str, body: str, *, declared_name: str | None = None
) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {declared_name or name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    _write_skill(tmp_path, "alpha", "Use when testing alpha.", "# Alpha\n\nBody A.")
    _write_skill(tmp_path, "beta", "Use when testing beta.", "# Beta\n\nBody B.")
    return tmp_path


def test_catalog_lists_names_without_bodies(skills_dir):
    tool = LoadSkillTool(skills_dir)
    assert "alpha" in tool.description
    assert "Use when testing alpha." in tool.description
    assert "Body A." not in tool.description


def test_ignores_directories_without_skill_md(skills_dir):
    (skills_dir / "notes.txt").write_text("not a skill", encoding="utf-8")
    (skills_dir / "empty").mkdir()
    tool = LoadSkillTool(skills_dir)
    assert "notes" not in tool.description
    assert "empty" not in tool.description


def test_frontmatter_name_must_match_directory(skills_dir):
    _write_skill(skills_dir, "mismatch", "desc", "body", declared_name="other")
    with pytest.raises(ValueError):
        LoadSkillTool(skills_dir)


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
        LoadSkillTool(tmp_path / "does-not-exist")


@pytest.mark.asyncio
async def test_loads_body_without_frontmatter(skills_dir):
    tool = LoadSkillTool(skills_dir)
    output = await tool.run(input=LoadSkillInput(name="alpha")).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result.startswith("# Alpha")
    assert "Body A." in result
    assert "name: alpha" not in result


@pytest.mark.asyncio
async def test_omitting_name_lists_skills(skills_dir):
    tool = LoadSkillTool(skills_dir)
    output = await tool.run(input=LoadSkillInput()).middleware(GlobalTrajectoryMiddleware(pretty=True))
    assert "alpha" in output.result
    assert "beta" in output.result


@pytest.mark.asyncio
async def test_unknown_skill_raises(skills_dir):
    tool = LoadSkillTool(skills_dir)
    with pytest.raises(ToolError):
        await tool.run(input=LoadSkillInput(name="does-not-exist")).middleware(
            GlobalTrajectoryMiddleware(pretty=True)
        )


def test_triage_skills_are_wired():
    """get_skills_dir() resolves the real skills and both triage skills load.

    Guards the silent failure mode: a wrong directory or filename would leave
    the tool with an empty catalog rather than raising.
    """
    tool = LoadSkillTool(get_skills_dir())
    assert "triage-validate-fix" in tool.description
    assert "triage-rebuild" in tool.description
