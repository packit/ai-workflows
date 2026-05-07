import logging
import os
import re
import subprocess

from beeai_framework.adapters.mcp.serve.server import (
    MCPServer,
    MCPServerConfig,
    MCPSettings,
)

from ymir.common.base_utils import parse_klist_principals
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
from ymir.tools.privileged.logdetective import AnalyzeLogsTool
from ymir.tools.privileged.lookaside import (
    DownloadSourcesTool,
    PrepSourcesTool,
    UploadSourcesTool,
)
from ymir.tools.privileged.maintainer_rules import MaintainerRulesTool
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


def _kerberos_principal() -> str | None:
    """Return the first non-expired principal from the Kerberos ticket cache, or None."""
    try:
        result = subprocess.run(["klist", "-l"], capture_output=True, text=True, timeout=10)
        principals = parse_klist_principals(result.stdout)
        return principals[0] if principals else None
    except Exception:
        pass
    return None


def check_distgit_ssh_access() -> None:
    """Verify SSH access to dist-git at startup via the bastion jump host.

    SSHs to pkgs.devel.redhat.com without an explicit username so the SSH
    config is exercised as-is, then compares gitolite's 'hello <user>'
    greeting against the Kerberos principal.  A mismatch means the SSH
    config User setting is wrong — easy to miss after a service-account
    rename.
    """
    principal = _kerberos_principal()
    if principal is None:
        logger.debug("No Kerberos principal found, skipping dist-git SSH access check")
        return
    krb_username = principal.split("@")[0]

    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "pkgs.devel.redhat.com"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (result.stdout + result.stderr).strip()
    match = re.search(r"hello (\S+),", output)
    if not match:
        if result.returncode != 0:
            raise RuntimeError(f"Dist-git SSH check failed (exit code {result.returncode}): {output}")
        raise RuntimeError(
            f"Dist-git SSH check: connected but could not parse gitolite greeting. Output: {output}"
        )
    ssh_username = match.group(1)
    if ssh_username != krb_username:
        raise RuntimeError(
            f"Dist-git SSH username mismatch: SSH authenticated as '{ssh_username}' but "
            f"Kerberos principal is '{krb_username}'. "
            "Fix the User setting for pkgs.devel.redhat.com in the SSH config."
        )
    logger.info("Dist-git SSH access verified: authenticated as '%s'", ssh_username)


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
    check_distgit_ssh_access()
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
            AnalyzeLogsTool(),
            MaintainerRulesTool(),
        ]
    )

    mcp.serve()


if __name__ == "__main__":
    main()
