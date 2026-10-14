"""Generic on-demand prompt reference loader.

Agents can keep large, branch-specific instruction blocks out of their
always-loaded prompt and instead expose them as named *references* that are
loaded only when needed. Each reference is a markdown file with YAML
frontmatter:

    ---
    name: validate_fix
    description: Use when ... (one or two lines)
    ---
    <instruction body>

``LoadPromptTool`` takes a references directory, builds a short catalog
(name + description) into its tool description so the model knows what is
available up front, and returns a body only when the model asks for it by
name.
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

DEFAULT_TOOL_NAME = "load_prompt"


class LoadPromptInput(BaseModel):
    name: str | None = Field(
        default=None,
        description=(
            "Name of the prompt reference to load. Omit to list the available "
            "references with their descriptions."
        ),
    )


class PromptReference(BaseModel):
    name: str
    description: str
    path: Path


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---``-delimited YAML frontmatter block from a fragment body."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            header = yaml.safe_load("".join(lines[1:index])) or {}
            body = "".join(lines[index + 1 :]).lstrip("\n")
            return header, body
    return {}, text


def load_references(directory: Path) -> list[PromptReference]:
    references = []
    for path in sorted(directory.glob("*.md")):
        meta, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        description = (meta.get("description") or "").strip()
        if not description:
            raise ValueError(f"Prompt reference {path} is missing a frontmatter description")
        references.append(
            PromptReference(name=str(meta.get("name") or path.stem), description=description, path=path)
        )
    return references


class LoadPromptTool(Tool[LoadPromptInput, ToolRunOptions, StringToolOutput]):
    name = DEFAULT_TOOL_NAME
    description = ""
    input_schema = LoadPromptInput

    def __init__(self, references_dir: Path, *, tool_name: str = DEFAULT_TOOL_NAME) -> None:
        super().__init__()
        directory = Path(references_dir)
        if not directory.is_dir():
            raise ValueError(f"Prompt references directory does not exist: {directory}")
        self.name = tool_name
        self._references = {reference.name: reference for reference in load_references(directory)}
        self.description = self._build_description()

    def _build_description(self) -> str:
        if not self._references:
            return "Load a named prompt reference. No references are currently available."
        lines = [
            "Load a detailed instruction reference on demand. These keep large,"
            " branch-specific procedures out of the main prompt; call this with `name`"
            " set to one of the following when you reach that part of the task:",
            "",
        ]
        lines.extend(
            f"- {reference.name}: {reference.description}" for reference in self._references.values()
        )
        return "\n".join(lines)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "load_prompt"],
            creator=self,
        )

    async def _run(
        self,
        input: LoadPromptInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        if input.name is None:
            return StringToolOutput(result=self._build_description())
        reference = self._references.get(input.name)
        if reference is None:
            available = ", ".join(self._references) or "none"
            raise ToolError(f"Unknown prompt reference {input.name!r}. Available: {available}")
        _, body = split_frontmatter(reference.path.read_text(encoding="utf-8"))
        return StringToolOutput(result=body)
