import logging
import os
import re

from beeai_framework.adapters.mcp.serve.server import (
    MCPServer,
    MCPServerConfig,
    MCPSettings,
)

from ymir.tools.gateway_utils import setup_logging
from ymir.tools.privileged.copr import BuildPackageTool, DownloadArtifactsTool
from ymir.tools.privileged.distgit import CreateZstreamBranchTool
from ymir.tools.privileged.gitlab import (
    AddBlockingMergeRequestCommentTool,
    AddMergeRequestCommentTool,
    AddMergeRequestLabelsTool,
    CloneRepositoryTool,
    FetchGitlabMrNotesTool,
    ForkRepositoryTool,
    GetAuthorizedCommentsFromMergeRequestTool,
    GetFailedPipelineJobsFromMergeRequestTool,
    GetInternalRhelBranchesTool,
    GetMergeRequestDetailsTool,
    GetPatchFromUrlTool,
    OpenMergeRequestTool,
    PushToRemoteRepositoryTool,
    RetryPipelineJobTool,
)
from ymir.tools.privileged.jira import (
    AddJiraCommentTool,
    ChangeJiraStatusTool,
    CheckCveTriageEligibilityTool,
    EditJiraLabelsTool,
    GetJiraDetailsTool,
    GetJiraDevStatusTool,
    GetJiraPullRequestsTool,
    SearchJiraIssuesTool,
    SetJiraFieldsTool,
    SetPreliminaryTestingTool,
    VerifyIssueAuthorTool,
)
from ymir.tools.privileged.lookaside import (
    DownloadSourcesTool,
    PrepSourcesTool,
    UploadSourcesTool,
)
from ymir.tools.privileged.zstream_search import ZStreamSearchTool

# Patterns that match common credential formats in log output
_REDACT_PATTERNS = [
    # GitLab PAT
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    # Anthropic API key
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    # Google API key
    re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"),
    # Bearer tokens in URLs or strings
    re.compile(r"oauth2:[^@\s]+@"),
    # Testing Farm API tokens (UUID format)
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE),
    # Jira Cloud API tokens (ATATT3x... pattern)
    re.compile(r"ATATT3x[A-Za-z0-9_-]{20,}"),
    # Base64 Authorization headers
    re.compile(r"Basic [A-Za-z0-9+/=]{20,}"),
    # Generic long hex/base64 tokens (e.g. Jira PATs)
    re.compile(
        r"(?:token|key|password|secret|credential)[\"'=:\s]+[A-Za-z0-9+/=_-]{20,}['\"\s]*",
        re.IGNORECASE,
    ),
]

logger = logging.getLogger(__name__)


def _redact(text: str) -> str:
    """Replace credential-like patterns in text with [REDACTED]."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def main():
    transport = os.getenv("MCP_TRANSPORT", "sse")
    config_kwargs = {"name": "Ymir Privileged MCP Gateway", "transport": transport}
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
            BuildPackageTool(),
            DownloadArtifactsTool(),
            CreateZstreamBranchTool(),
            AddBlockingMergeRequestCommentTool(),
            AddMergeRequestCommentTool(),
            AddMergeRequestLabelsTool(),
            CloneRepositoryTool(),
            ForkRepositoryTool(),
            GetAuthorizedCommentsFromMergeRequestTool(),
            GetFailedPipelineJobsFromMergeRequestTool(),
            GetInternalRhelBranchesTool(),
            GetMergeRequestDetailsTool(),
            GetPatchFromUrlTool(),
            OpenMergeRequestTool(),
            PushToRemoteRepositoryTool(),
            RetryPipelineJobTool(),
            FetchGitlabMrNotesTool(),
            AddJiraCommentTool(),
            ChangeJiraStatusTool(),
            CheckCveTriageEligibilityTool(),
            EditJiraLabelsTool(),
            GetJiraDetailsTool(),
            GetJiraDevStatusTool(),
            GetJiraPullRequestsTool(),
            SearchJiraIssuesTool(),
            SetJiraFieldsTool(),
            SetPreliminaryTestingTool(),
            VerifyIssueAuthorTool(),
            DownloadSourcesTool(),
            PrepSourcesTool(),
            UploadSourcesTool(),
            ZStreamSearchTool(),
        ]
    )

    mcp.serve()


if __name__ == "__main__":
    main()
