import os

from beeai_framework.adapters.mcp.serve.server import (
    MCPServer,
    MCPServerConfig,
    MCPSettings,
)

from ymir.common.mock_repos import apply_zstream_override_from_env
from ymir.tools.gateway_utils import setup_logging
from ymir.tools.unprivileged.analyze_ewa_testrun import AnalyzeEwaTestRunTool
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.distgit_detector import DistgitDetectorTool
from ymir.tools.unprivileged.filesystem import GetCWDTool, RemoveTool
from ymir.tools.unprivileged.greenwave import FetchGreenWaveTool, FetchTestingFarmResultsTool
from ymir.tools.unprivileged.read_logfile import ReadLogfileTool
from ymir.tools.unprivileged.read_readme import ReadReadmeTool
from ymir.tools.unprivileged.search_resultsdb import SearchResultsdbTool
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
    BuildSrpmTool,
    GitLogSearchTool,
    GitPatchApplyFinishTool,
    GitPatchApplyTool,
    GitPatchCreationTool,
    GitPreparePackageSources,
    RunPackagePrepTool,
)


def main():
    transport = os.getenv("MCP_TRANSPORT", "sse")
    config_kwargs = {"name": "Ymir Unprivileged MCP Gateway", "transport": transport}
    if transport == "sse":
        config_kwargs["settings"] = MCPSettings(
            host="0.0.0.0",  # noqa: S104
            port=int(os.getenv("SSE_PORT", "8000")),
        )
    config = MCPServerConfig(**config_kwargs)

    setup_logging()
    apply_zstream_override_from_env()
    # Shared options dict so tools can communicate cross-step state
    # (e.g. base_head_commit set by GitPreparePackageSources / ApplyDownstreamPatchesTool,
    # read by GitPatchCreationTool).
    tool_options: dict = {"working_directory": None}
    mcp = MCPServer(config=config)
    mcp.register_many(
        [
            RunShellCommandTool(options=tool_options),
            DistgitDetectorTool(options=tool_options),
            GetCWDTool(options=tool_options),
            RemoveTool(options=tool_options),
            GetPackageInfoTool(options=tool_options),
            AddChangelogEntryTool(options=tool_options),
            UpdateReleaseTool(options=tool_options),
            CreateTool(options=tool_options),
            ViewTool(options=tool_options),
            InsertTool(options=tool_options),
            InsertAfterSubstringTool(options=tool_options),
            StrReplaceTool(options=tool_options),
            SearchTextTool(options=tool_options),
            UpstreamSearchTool(options=tool_options),
            ExtractUpstreamRepositoryTool(options=tool_options),
            CloneUpstreamRepositoryTool(options=tool_options),
            FindBaseCommitTool(options=tool_options),
            ApplyDownstreamPatchesTool(options=tool_options),
            CherryPickCommitTool(options=tool_options),
            CherryPickContinueTool(options=tool_options),
            VersionMapperTool(options=tool_options),
            GitPatchApplyTool(options=tool_options),
            GitPatchApplyFinishTool(options=tool_options),
            GitPatchCreationTool(options=tool_options),
            GitLogSearchTool(options=tool_options),
            GitPreparePackageSources(options=tool_options),
            RunPackagePrepTool(options=tool_options),
            BuildSrpmTool(options=tool_options),
            AnalyzeEwaTestRunTool(options=tool_options),
            FetchGreenWaveTool(options=tool_options),
            FetchTestingFarmResultsTool(options=tool_options),
            ReadLogfileTool(options=tool_options),
            ReadReadmeTool(options=tool_options),
            SearchResultsdbTool(options=tool_options),
        ]
    )

    mcp.serve()


if __name__ == "__main__":
    main()
