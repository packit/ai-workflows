from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions


class VersionMapperInput(BaseModel):
    major_version: int = Field(description="RHEL major version (e.g., 8, 9, 10)")
    is_critical: bool = Field(description="Whether this is a most critical issue requiring Z-stream (e.g., privilege escalation, remote code execution, data loss)", default=False)


class VersionMapperResult(BaseModel):
    fix_version: str = Field(description="The appropriate fix version for the given major version and criticality")


class VersionMapperOutput(JSONToolOutput[VersionMapperResult]):
    pass


class VersionMapperTool(Tool[VersionMapperInput, ToolRunOptions, VersionMapperOutput]):
    """Tool to map RHEL major versions to current development fix versions."""

    name = "map_version"
    description = "Map RHEL major version to current development fix version (Y-stream or Z-stream for most critical issues only)."
    input_schema = VersionMapperInput

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "version", "mapper"],
            creator=self,
        )

    async def _run(
        self, tool_input: VersionMapperInput, options: ToolRunOptions | None, context: RunContext
    ) -> VersionMapperOutput:
        """
        Map RHEL major version to the appropriate fix version.

        Args:
            tool_input: Input containing major_version and is_critical

        Returns:
            VersionMapperOutput with fix_version
        """
        major_version = tool_input.major_version
        is_critical = tool_input.is_critical

        if major_version == 8:
            fix_version = "rhel-8.10.z"
        elif major_version == 9:
            if is_critical:
                fix_version = "rhel-9.7.z"
            else:
                fix_version = "rhel-9.8"
        elif major_version == 10:
            if is_critical:
                fix_version = "rhel-10.1.z"
            else:
                fix_version = "rhel-10.2"
        else:
            raise ValueError(f"Unsupported RHEL major version: {major_version}. Supported versions: 8, 9, 10")

        result = VersionMapperResult(
            fix_version=fix_version
        )

        return VersionMapperOutput(result=result)
