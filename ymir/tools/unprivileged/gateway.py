import logging
import os
import re
from typing import Any

from beeai_framework.adapters.mcp.serve.server import (
    MCPServer,
    MCPServerConfig,
    MCPSettings,
)
from beeai_framework.emitter.emitter import Emitter

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


def _setup_logging():
    logging.basicConfig(level=logging.INFO)

    # Log tool calls via Emitter.
    # Dotted strings in Emitter.on() are matched exactly (not as globs),
    # so we use regex patterns to match any tool's events.
    def on_tool_start(data: Any, meta: Any):
        logger.info(f"Tool called: {meta.creator}")
        logger.info(f"Tool arguments: {_redact(str(data))}")

    def on_tool_success(data: Any, meta: Any):
        logger.info(f"Tool {meta.creator} completed successfully")

    def on_tool_error(data: Any, meta: Any):
        logger.error(f"Tool {meta.creator} failed with error: {_redact(str(data))}")
        error = getattr(data, "error", None)
        if error is not None:
            logger.error(f"Tool {meta.creator} traceback:", exc_info=error)

    Emitter.root().on(re.compile(r"^tool\..+\.start$"), on_tool_start)
    Emitter.root().on(re.compile(r"^tool\..+\.success$"), on_tool_success)
    Emitter.root().on(re.compile(r"^tool\..+\.error$"), on_tool_error)


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
