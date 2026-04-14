import logging
import os
from typing import Any

from beeai_framework.adapters.mcp.serve.server import MCPServer, MCPServerConfig, MCPSettings
from beeai_framework.emitter.emitter import Emitter

from ymir_tools.unprivileged.commands import RunShellCommandTool
from ymir_tools.unprivileged.distgit_detector import DistgitDetectorTool
from ymir_tools.unprivileged.filesystem import GetCWDTool, RemoveTool
from ymir_tools.unprivileged.specfile import AddChangelogEntryTool, GetPackageInfoTool, UpdateReleaseTool
from ymir_tools.unprivileged.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    SearchTextTool,
    StrReplaceTool,
    ViewTool,
)
from ymir_tools.unprivileged.upstream_search import UpstreamSearchTool
from ymir_tools.unprivileged.upstream_tools import (
    ApplyDownstreamPatchesTool,
    CherryPickCommitTool,
    CherryPickContinueTool,
    CloneUpstreamRepositoryTool,
    ExtractUpstreamRepositoryTool,
    FindBaseCommitTool,
    GeneratePatchFromCommitTool,
)
from ymir_tools.unprivileged.version_mapper import VersionMapperTool
from ymir_tools.unprivileged.wicked_git import (
    GitLogSearchTool,
    GitPatchApplyFinishTool,
    GitPatchApplyTool,
    GitPatchCreationTool,
)

logger = logging.getLogger(__name__)

def _setup_logging():
    logging.basicConfig(level=logging.INFO)

    def on_tool_start(data: Any, meta: Any):
        logger.info(f"Tool called: {meta.name}")
        logger.info(f"Tool arguments: {data}")

    def on_tool_success(data: Any, meta: Any):
        logger.info(f"Tool {meta.name} completed successfully")

    def on_tool_error(data: Any, meta: Any):
        logger.error(f"Tool {meta.name} failed with error: {data}")
        error = getattr(data, "error", None)
        if error is not None:
            logger.error(f"Tool {meta.name} traceback:", exc_info=error)

    Emitter.root().on("tool.*.start", on_tool_start)
    Emitter.root().on("tool.*.success", on_tool_success)
    Emitter.root().on("tool.*.error", on_tool_error)


def main():
    transport = os.getenv("MCP_TRANSPORT", "sse")
    config_kwargs = {"name": "Ymir Unprivileged MCP Gateway", "transport": transport}
    if transport == "sse":
        config_kwargs["settings"] = MCPSettings(
            host="0.0.0.0",
            port=int(os.getenv("SSE_PORT", "8000")),
        )
    config = MCPServerConfig(**config_kwargs)

    _setup_logging()
    mcp = MCPServer(config=config)
    mcp.register_many([
        RunShellCommandTool(),
        DistgitDetectorTool(),
        GetCWDTool(),
        RemoveTool(),
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
    ])

    mcp.serve()


if __name__ == "__main__":
    main()
