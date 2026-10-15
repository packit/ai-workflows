"""Generic on-demand Agent Skill loader.

Agents can keep large, branch-specific instruction blocks out of their
always-loaded prompt and instead expose them as Agent Skills that are loaded
only when needed. A skill is a directory containing a ``SKILL.md`` with YAML
frontmatter in the standard Agent Skills shape:

    skills/triage-rebuild/SKILL.md
    ---
    name: triage-rebuild
    description: Use when ... (one or two lines)
    ---
    <instruction body>

``LoadSkillTool`` takes a skills directory, builds a short catalog
(name + description) into its tool description so the model knows what is
available up front, and returns a body only when the model asks for it by
name. The frontmatter shape is the Agent Skills standard, so the same files
can be consumed by any skill-aware harness, not just BeeAI.
"""

import logging
from pathlib import Path

import yaml
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.tools.base import CloneableTool as Tool

logger = logging.getLogger(__name__)

DEFAULT_TOOL_NAME = "load_skill"


class LoadSkillInput(BaseModel):
    name: str | None = Field(
        default=None,
        description=("Name of the skill to load. Omit to list the available skills with their descriptions."),
    )


class Skill(BaseModel):
    name: str
    description: str
    path: Path


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---``-delimited YAML frontmatter block from a skill body."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            header = yaml.safe_load("".join(lines[1:index])) or {}
            body = "".join(lines[index + 1 :]).lstrip("\n")
            return header, body
    return {}, text


def load_skills(directory: Path) -> list[Skill]:
    """Load every ``<name>/SKILL.md`` under *directory*.

    Each skill's frontmatter ``name`` must match its directory name, as the
    Agent Skills standard requires.
    """
    skills = []
    for path in sorted(directory.glob("*/SKILL.md")):
        meta, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        description = (meta.get("description") or "").strip()
        name = str(meta.get("name") or "").strip()
        if not description or not name:
            raise ValueError(f"Skill {path} needs frontmatter 'name' and 'description'")
        if name != path.parent.name:
            raise ValueError(f"Skill {path} declares name {name!r} but its directory is {path.parent.name!r}")
        skills.append(Skill(name=name, description=description, path=path))
    return skills


class LoadSkillTool(Tool[LoadSkillInput, ToolRunOptions, StringToolOutput]):
    name = DEFAULT_TOOL_NAME
    description = ""
    input_schema = LoadSkillInput

    def __init__(self, skills_dir: Path, *, tool_name: str = DEFAULT_TOOL_NAME) -> None:
        super().__init__()
        directory = Path(skills_dir)
        if not directory.is_dir():
            raise ValueError(f"Skills directory does not exist: {directory}")
        self.name = tool_name
        self._skills = {skill.name: skill for skill in load_skills(directory)}
        self.description = self._build_description()

    def _build_description(self) -> str:
        if not self._skills:
            return "Load a named skill. No skills are currently available."
        lines = [
            "Load a detailed skill on demand. These keep large, branch-specific"
            " procedures out of the main prompt; call this with `name` set to one of"
            " the following when you reach that part of the task:",
            "",
        ]
        lines.extend(f"- {skill.name}: {skill.description}" for skill in self._skills.values())
        return "\n".join(lines)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "load_skill"],
            creator=self,
        )

    async def _run(
        self,
        input: LoadSkillInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        if input.name is None:
            return StringToolOutput(result=self._build_description())
        skill = self._skills.get(input.name)
        if skill is None:
            available = ", ".join(self._skills) or "none"
            raise ToolError(f"Unknown skill {input.name!r}. Available: {available}")
        _, body = split_frontmatter(skill.path.read_text(encoding="utf-8"))
        return StringToolOutput(result=body)
