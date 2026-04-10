import logging
import os
from typing import Any

from beeai_framework.adapters.mcp.serve.server import MCPServer, MCPServerConfig, MCPSettings
from beeai_framework.emitter.emitter import Emitter

from commands import RunShellCommandTool
from distgit_detector import DistgitDetectorTool
from filesystem import GetCWDTool, RemoveTool
from patch_validator import PatchValidatorTool
from specfile import AddChangelogEntryTool, GetPackageInfoTool, UpdateReleaseTool
from text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    SearchTextTool,
    StrReplaceTool,
    ViewTool,
)
from upstream_search import UpstreamSearchTool
from upstream_tools import (
    ApplyDownstreamPatchesTool,
    CherryPickCommitTool,
    CherryPickContinueTool,
    CloneUpstreamRepositoryTool,
    ExtractUpstreamRepositoryTool,
    FindBaseCommitTool,
    GeneratePatchFromCommitTool,
)
from version_mapper import VersionMapperTool
from wicked_git import (
    GitLogSearchTool,
    GitPatchApplyFinishTool,
    GitPatchApplyTool,
    GitPatchCreationTool,
)
from zstream_search import ZStreamSearchTool


def _setup_logging():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("FastMCP").handlers = [logging.StreamHandler()]

    def on_tool_start(data: Any, meta: Any):
        logger.info(f"Tool called: {meta.name}")
        logger.info(f"Tool arguments: {data}")

    def on_tool_success(data: Any, meta: Any):
        logger.info(f"Tool {meta.name} completed successfully")

    def on_tool_error(data: Any, meta: Any):
        logger.error(f"Tool {meta.name} failed with error: {data}")

    Emitter.root().on("tool.*.start", on_tool_start)
    Emitter.root().on("tool.*.success", on_tool_success)
    Emitter.root().on("tool.*.error", on_tool_error)


if __name__ == "__main__":
    logger = logging.getLogger(__name__)

    config = MCPServerConfig(
        name="MCP Gateway",
        transport="sse",
        settings=MCPSettings(
            host="0.0.0.0",
            port=int(os.getenv("SSE_PORT", "8000")),
        )
    )

    _setup_logging()
    mcp = MCPServer(config=config)
    mcp.register_many([
        RunShellCommandTool(),
        DistgitDetectorTool(),
        GetCWDTool(),
        RemoveTool(),
        PatchValidatorTool(),
        GetPackageInfoTool(),
        AddChangelogEntryTool(),
        UpdateReleaseTool(),
        CreateTool(),
        ViewTool(),
        InsertTool(),
        InsertAfterSubstringTool(),
        StrReplaceTool(),
        SearchTextTool(),
        UpstreamSearchTool(),
        ExtractUpstreamRepositoryTool(),
        CloneUpstreamRepositoryTool(),
        FindBaseCommitTool(),
        ApplyDownstreamPatchesTool(),
        CherryPickCommitTool(),
        CherryPickContinueTool(),
        GeneratePatchFromCommitTool(),
        VersionMapperTool(),
        GitPatchApplyTool(),
        GitPatchApplyFinishTool(),
        GitPatchCreationTool(),
        GitLogSearchTool(),
        ZStreamSearchTool(),
    ])

    mcp.serve()
