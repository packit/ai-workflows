from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions

from common.config import load_rhel_config


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
        Map RHEL major version to the appropriate fix version using rhel-config.json.

        Args:
            tool_input: Input containing major_version and is_critical

        Returns:
            VersionMapperOutput with fix_version
        """
        major_version = tool_input.major_version
        is_critical = tool_input.is_critical
        major_version_str = str(major_version)

        config = await load_rhel_config()

        z_streams = config.get("current_z_streams", {})
        y_streams = config.get("current_y_streams", {})

        if is_critical:
            fix_version = z_streams.get(major_version_str)
            if not fix_version:
                raise ValueError(
                    f"Unsupported RHEL major version for Z-stream: {major_version}. "
                    f"Available Z-stream versions: {z_streams.keys()}"
                )
        else:
            fix_version = y_streams.get(major_version_str)
            if not fix_version:
                # No Y-stream available (e.g., RHEL 8), use Z-stream instead
                fix_version = z_streams.get(major_version_str)

                if not fix_version:
                    raise ValueError(
                        f"Unsupported RHEL major version: {major_version}. "
                        f"Available versions - Y-stream: {y_streams.keys()}, Z-stream: {z_streams.keys()}"
                    )

        return VersionMapperResult(fix_version=fix_version)
