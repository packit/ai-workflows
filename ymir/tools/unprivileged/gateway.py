import os

from beeai_framework.adapters.mcp.serve.server import (
    MCPServer,
    MCPServerConfig,
    MCPSettings,
)

from ymir.tools.gateway_utils import setup_logging
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.distgit_detector import DistgitDetectorTool
from ymir.tools.unprivileged.filesystem import GetCWDTool, RemoveTool
from ymir.tools.unprivileged.specfile import (
    AddChangelogEntryTool,
    GetPackageInfoTool,
    UpdateReleaseTool,
)
from ymir.tools.unprivileged.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    SearchTextTool,
    StrReplaceTool,
    ViewTool,
)
from ymir.tools.unprivileged.upstream_search import UpstreamSearchTool
from ymir.tools.unprivileged.upstream_tools import (
    ApplyDownstreamPatchesTool,
    CherryPickCommitTool,
    CherryPickContinueTool,
    CloneUpstreamRepositoryTool,
    ExtractUpstreamRepositoryTool,
    FindBaseCommitTool,
)
from ymir.tools.unprivileged.version_mapper import VersionMapperTool
from ymir.tools.unprivileged.wicked_git import (
    GitLogSearchTool,
    GitPatchApplyFinishTool,
    GitPatchApplyTool,
    GitPatchCreationTool,
)


def main():
    transport = os.getenv("MCP_TRANSPORT", "sse")
    config_kwargs = {"name": "Ymir Unprivileged MCP Gateway", "transport": transport}
    if transport == "sse":
        config_kwargs["settings"] = MCPSettings(
            host="0.0.0.0",
            port=int(os.getenv("SSE_PORT", "8000")),
        )
    config = MCPServerConfig(**config_kwargs)

    setup_logging()
    mcp = MCPServer(config=config)
    mcp.register_many(
        [
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
            VersionMapperTool(),
            GitPatchApplyTool(),
            GitPatchApplyFinishTool(),
            GitPatchCreationTool(),
            GitLogSearchTool(),
        ]
    )

    mcp.serve()


if __name__ == "__main__":
    main()
