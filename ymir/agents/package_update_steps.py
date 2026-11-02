import logging
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ymir.common.models import LogOutputSchema

logger = logging.getLogger(__name__)


class PackageUpdateState(BaseModel):
    jira_issue: str | None
    workspace_id: UUID = Field(default_factory=uuid4)
    package: str
    dist_git_branch: str
    dist_git_namespace: str | None = Field(
        default=None,
        description=(
            "Explicit GitLab rpms namespace (rhel or centos-stream) when the branch "
            "name is ambiguous (modular stream-* branches)."
        ),
    )
    local_clone: Path | None = Field(default=None)
    spec_name: str | None = Field(
        default=None, description="Actual spec filename (may differ from package name)"
    )
    update_branch: str | None = Field(default=None)
    fork_url: str | None = Field(default=None)
    build_error: str | None = Field(default=None)
    build_url: str | None = Field(
        default=None, description="Link to the last submitted Copr build, for the Jira comment"
    )
    log_result: LogOutputSchema | None = Field(default=None)
    canonical_title: str | None = Field(default=None, description="Resolved title shared by the issue family")
    changelog_entry_count: int | None = Field(
        default=None,
        description="Explicit changelog entry count before the Log Agent runs; None for %autochangelog",
    )
    merge_request_url: str | None = Field(default=None)
    merge_request_newly_created: bool = Field(default=False)  # was the MR newly created?
    zstream_branch_created: str | None = Field(default=None)
    zstream_branch_warning: str | None = Field(default=None)


def build_mr_note(
    *,
    build_url: str | None,
    is_modular_issue: bool,
    module_stream: str | None,
) -> str:
    """Build information comment to post on the merge request.

    Includes the Copr build link (noting that Copr projects are temporary)
    and any modular-package notes so reviewers have the context where they
    review — on the MR, not in Jira.
    """
    parts: list[str] = []
    if build_url:
        parts.append(f"Copr build: {build_url}\n*(Copr projects are temporary — this link may expire.)*")
    if is_modular_issue:
        if module_stream:
            parts.append(
                f"Modular package — Copr build was attempted with "
                f"module stream `{module_stream}` enabled in the buildroot. "
                "Please verify the build result before merging."
            )
        else:
            parts.append(
                "Modular package — the `module:stream` could not be determined, "
                "so the Copr build ran without enabling a module stream. "
                "Please verify the MR and build result manually."
            )
    return "\n\n".join(parts)


class PackageUpdateStep:
    """
    Steps for package update operations (backport and rebase steps).

    A place where to share common steps between backport and rebase workflows.
    """
