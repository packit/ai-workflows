from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions

from common.config import load_rhel_config


class VersionMapperInput(BaseModel):
    major_version: int = Field(description="RHEL major version (e.g., 8, 9, 10)")


class VersionMapperResult(BaseModel):
    y_stream: str | None = Field(description="Current Y-stream version for the major version (None if no Y-stream available)")
    z_stream: str | None = Field(description="Current Z-stream version for the major version (None if no Z-stream available)")
    is_maintenance_version: bool = Field(description="True if this is a maintenance version (no Y-stream available)")


class VersionMapperOutput(JSONToolOutput[VersionMapperResult]):
    pass


class VersionMapperTool(Tool[VersionMapperInput, ToolRunOptions, VersionMapperOutput]):
    """Tool to map RHEL major versions to current development fix versions."""

    name = "map_version"
    description = "Map RHEL major version to current Y-stream and Z-stream versions. Returns both streams for LLM to decide based on criticality."
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
        Map RHEL major version to current Y-stream and Z-stream versions.

        Args:
            tool_input: Input containing major_version

        Returns:
            VersionMapperOutput with y_stream, z_stream, and is_maintenance_version
        """
        major_version = tool_input.major_version
        major_version_str = str(major_version)

        config = await load_rhel_config()

        upcoming_z_streams = config.get("upcoming_z_streams", {})
        current_z_streams = config.get("current_z_streams", {})
        current_y_streams = config.get("current_y_streams", {})

        y_stream = current_y_streams.get(major_version_str)
        # 0-day Z-Stream during stabilization phase, regular Z-Stream otherwise
        z_stream = upcoming_z_streams.get(major_version_str) or current_z_streams.get(major_version_str)
        is_maintenance_version = y_stream is None

        return VersionMapperOutput(VersionMapperResult(
            y_stream=y_stream,
            z_stream=z_stream,
            is_maintenance_version=is_maintenance_version
        ))
