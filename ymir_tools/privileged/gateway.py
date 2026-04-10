import logging
import re
from typing import Any
import os

from beeai_framework.adapters.mcp.serve.server import MCPServer, MCPServerConfig, MCPSettings
from beeai_framework.emitter.emitter import Emitter


logger = logging.getLogger(__name__)

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
    re.compile(r"(?:token|key|password|secret|credential)[\"'=:\s]+[A-Za-z0-9+/=_-]{20,}['\"\s]*", re.IGNORECASE),
]


def _redact(text: str) -> str:
    """Replace credential-like patterns in text with [REDACTED]."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text

from ymir_tools.privileged.copr_tools import BuildPackageTool, DownloadArtifactsTool
from ymir_tools.privileged.distgit_tools import CreateZstreamBranchTool
from ymir_tools.privileged.gitlab_tools import (
    AddBlockingMergeRequestCommentTool,
    AddMergeRequestCommentTool,
    AddMergeRequestLabelsTool,
    CloneRepositoryTool,
    CreateMergeRequestChecklistTool,
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
from ymir_tools.privileged.jira_tools import (
    AddJiraCommentTool,
    ChangeJiraStatusTool,
    CheckCveTriageEligibilityTool,
    EditJiraLabelsTool,
    GetJiraDetailsTool,
    GetJiraDevStatusTool,
    SearchJiraIssuesTool,
    SetJiraFieldsTool,
    VerifyIssueAuthorTool,
)
from ymir_tools.privileged.lookaside_tools import DownloadSourcesTool, PrepSourcesTool, UploadSourcesTool


def _setup_logging():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("FastMCP").handlers = [logging.StreamHandler()]

    # Log tool calls via Emitter
    def on_tool_start(data: Any, meta: Any):
        logger.info(f"Tool called: {meta.name}")
        logger.info(f"Tool arguments: {_redact(str(data))}")

    def on_tool_success(data: Any, meta: Any):
        logger.info(f"Tool {meta.name} completed successfully")

    def on_tool_error(data: Any, meta: Any):
        logger.error(f"Tool {meta.name} failed with error: {_redact(str(data))}")

    Emitter.root().on("tool.*.start", on_tool_start)
    Emitter.root().on("tool.*.success", on_tool_success)
    Emitter.root().on("tool.*.error", on_tool_error)


def main():
    logger = logging.getLogger(__name__)

    transport = os.getenv("MCP_TRANSPORT", "sse")
    config_kwargs = {"name": "Ymir Privileged MCP Gateway", "transport": transport}
    if transport == "sse":
        config_kwargs["settings"] = MCPSettings(
            host="0.0.0.0",
            port=int(os.getenv("SSE_PORT", "8000")),
        )
    config = MCPServerConfig(**config_kwargs)

    _setup_logging()
    mcp = MCPServer(config=config)
    mcp.register_many([
        BuildPackageTool(),
        DownloadArtifactsTool(),
        CreateZstreamBranchTool(),
        AddBlockingMergeRequestCommentTool(),
        AddMergeRequestCommentTool(),
        AddMergeRequestLabelsTool(),
        CloneRepositoryTool(),
        CreateMergeRequestChecklistTool(),
        ForkRepositoryTool(),
        GetAuthorizedCommentsFromMergeRequestTool(),
        GetFailedPipelineJobsFromMergeRequestTool(),
        GetInternalRhelBranchesTool(),
        GetMergeRequestDetailsTool(),
        GetPatchFromUrlTool(),
        OpenMergeRequestTool(),
        PushToRemoteRepositoryTool(),
        RetryPipelineJobTool(),
        AddJiraCommentTool(),
        ChangeJiraStatusTool(),
        CheckCveTriageEligibilityTool(),
        EditJiraLabelsTool(),
        GetJiraDetailsTool(),
        GetJiraDevStatusTool(),
        SearchJiraIssuesTool(),
        SetJiraFieldsTool(),
        VerifyIssueAuthorTool(),
        DownloadSourcesTool(),
        PrepSourcesTool(),
        UploadSourcesTool(),
    ])

    mcp.serve()


if __name__ == "__main__":
    main()
