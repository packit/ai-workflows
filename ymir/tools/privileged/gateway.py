import asyncio
import logging
import os

from beeai_framework.adapters.mcp.serve.server import (
    MCPServer,
    MCPServerConfig,
    MCPSettings,
)

from ymir.common.mock_repos import apply_zstream_override_from_env
from ymir.tools.gateway_utils import get_log_detective_mcp, setup_logging
from ymir.tools.privileged.copr import BuildPackageTool, DownloadArtifactsTool
from ymir.tools.privileged.distgit import CreateZstreamBranchTool
from ymir.tools.privileged.errata import (
    ErratumAddCommentTool,
    ErratumChangeStateTool,
    ErratumPushToStageTool,
    ErratumRefreshSecurityAlertsTool,
    GetErratumBuildMapTool,
    GetErratumBuildNvrTool,
    GetErratumStagePushDetailsTool,
    GetErratumTool,
    GetErratumTransitionRulesTool,
    GetPreviousErratumTool,
)
from ymir.tools.privileged.gitlab import (
    AddBlockingMergeRequestCommentTool,
    AddMergeRequestCommentTool,
    AddMergeRequestLabelsTool,
    CloneRepositoryTool,
    FetchBranchTool,
    FetchCommitTool,
    FetchGitlabMrNotesTool,
    ForkRepositoryTool,
    GetAuthorizedCommentsFromMergeRequestTool,
    GetFailedPipelineJobsFromMergeRequestTool,
    GetInternalRhelBranchesTool,
    GetMergeRequestDetailsTool,
    GetPatchFromUrlTool,
    GetRemoteBranchHeadTool,
    ListProjectMergeRequestsTool,
    OpenMergeRequestTool,
    PushToRemoteRepositoryTool,
    ResolveQeReviewersTool,
    ResolveReviewersTool,
    RetryPipelineJobTool,
    SearchGitlabProjectMrsTool,
    SetMergeRequestReviewersTool,
)
from ymir.tools.privileged.jira import (
    AddJiraAttachmentsTool,
    AddJiraCommentTool,
    ChangeJiraStatusTool,
    CheckCveTriageEligibilityTool,
    CreateJiraIssueTool,
    EditJiraLabelsTool,
    GetJiraAttachmentTool,
    GetJiraDetailsTool,
    GetJiraDevStatusTool,
    GetJiraPullRequestsTool,
    SearchJiraIssuesTool,
    SetJiraFieldsTool,
    SetPreliminaryTestingTool,
    UpdateJiraCommentTool,
    VerifyIssueAuthorTool,
)
from ymir.tools.privileged.lookaside import (
    DownloadSourcesTool,
    UploadSourcesTool,
)
from ymir.tools.privileged.maintainer_rules import MaintainerRulesTool
from ymir.tools.privileged.testing_farm import (
    CancelTestingFarmRequestTool,
    CopyFilesToRemoteTool,
    GetTestingFarmRequestTool,
    GetTestingFarmReservationDetailsTool,
    ListTestingFarmComposesTool,
    ReproduceTestingFarmRequestTool,
    ReserveTestingFarmMachineTool,
    RunRemoteCommandTool,
)
from ymir.tools.privileged.zstream_search import ZStreamSearchTool

logger = logging.getLogger(__name__)


async def _async_main():
    transport = os.getenv("MCP_TRANSPORT", "sse")
    config_kwargs = {"name": "Ymir Privileged MCP Gateway", "transport": transport}
    if transport == "sse":
        config_kwargs["settings"] = MCPSettings(
            host="0.0.0.0",  # noqa: S104
            port=int(os.getenv("SSE_PORT", "8000")),
        )
    config = MCPServerConfig(**config_kwargs)

    setup_logging()
    apply_zstream_override_from_env()
    tool_options: dict = {"working_directory": None}
    mcp = MCPServer(config=config)

    log_detective_tools = await get_log_detective_mcp()
    if log_detective_tools:
        logger.info(
            "LogDetective MCP tools registered: %s",
            [t.name for t in log_detective_tools],
        )
    else:
        logger.info("Gateway starting without LogDetective MCP tools.")

    mcp.register_many(
        [
            BuildPackageTool(options=tool_options),
            DownloadArtifactsTool(options=tool_options),
            CreateZstreamBranchTool(options=tool_options),
            AddBlockingMergeRequestCommentTool(options=tool_options),
            AddMergeRequestCommentTool(options=tool_options),
            AddMergeRequestLabelsTool(options=tool_options),
            CloneRepositoryTool(options=tool_options),
            FetchBranchTool(options=tool_options),
            FetchCommitTool(options=tool_options),
            ForkRepositoryTool(options=tool_options),
            GetAuthorizedCommentsFromMergeRequestTool(options=tool_options),
            GetFailedPipelineJobsFromMergeRequestTool(options=tool_options),
            GetInternalRhelBranchesTool(options=tool_options),
            GetMergeRequestDetailsTool(options=tool_options),
            GetPatchFromUrlTool(options=tool_options),
            GetRemoteBranchHeadTool(options=tool_options),
            ListProjectMergeRequestsTool(options=tool_options),
            OpenMergeRequestTool(options=tool_options),
            PushToRemoteRepositoryTool(options=tool_options),
            RetryPipelineJobTool(options=tool_options),
            FetchGitlabMrNotesTool(options=tool_options),
            SearchGitlabProjectMrsTool(options=tool_options),
            ResolveReviewersTool(options=tool_options),
            ResolveQeReviewersTool(options=tool_options),
            SetMergeRequestReviewersTool(options=tool_options),
            GetErratumTool(options=tool_options),
            GetErratumBuildNvrTool(options=tool_options),
            GetErratumTransitionRulesTool(options=tool_options),
            GetErratumBuildMapTool(options=tool_options),
            GetPreviousErratumTool(options=tool_options),
            GetErratumStagePushDetailsTool(options=tool_options),
            ErratumPushToStageTool(options=tool_options),
            ErratumChangeStateTool(options=tool_options),
            ErratumAddCommentTool(options=tool_options),
            ErratumRefreshSecurityAlertsTool(options=tool_options),
            GetTestingFarmRequestTool(options=tool_options),
            ReproduceTestingFarmRequestTool(options=tool_options),
            ListTestingFarmComposesTool(options=tool_options),
            ReserveTestingFarmMachineTool(options=tool_options),
            GetTestingFarmReservationDetailsTool(options=tool_options),
            CancelTestingFarmRequestTool(options=tool_options),
            RunRemoteCommandTool(options=tool_options),
            CopyFilesToRemoteTool(options=tool_options),
            AddJiraAttachmentsTool(options=tool_options),
            AddJiraCommentTool(options=tool_options),
            ChangeJiraStatusTool(options=tool_options),
            CheckCveTriageEligibilityTool(options=tool_options),
            EditJiraLabelsTool(options=tool_options),
            GetJiraAttachmentTool(options=tool_options),
            GetJiraDetailsTool(options=tool_options),
            GetJiraDevStatusTool(options=tool_options),
            GetJiraPullRequestsTool(options=tool_options),
            SearchJiraIssuesTool(options=tool_options),
            SetJiraFieldsTool(options=tool_options),
            SetPreliminaryTestingTool(options=tool_options),
            UpdateJiraCommentTool(options=tool_options),
            VerifyIssueAuthorTool(options=tool_options),
            CreateJiraIssueTool(options=tool_options),
            DownloadSourcesTool(options=tool_options),
            UploadSourcesTool(options=tool_options),
            ZStreamSearchTool(options=tool_options),
            MaintainerRulesTool(options=tool_options),
            *log_detective_tools,
        ]
    )

    await mcp.aserve()


def main():
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
