"""
Pydantic models for Golang Rebuild Agent.

Uses common.models.Task for queue integration.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from ymir.agents.golang_rebuild.constants import RebuildStatus


class GolangRebuildData(BaseModel):
    """Triage routing metadata for golang rebuild tasks (stored in Task.metadata)."""

    golang_ticket: str = Field(description="Golang CVE ticket key (e.g., RHEL-158645)")
    component_ticket: str = Field(description="Component ticket key (e.g., RHEL-149580)")
    component: str = Field(description="Component name (e.g., buildah)")
    rhel_version: str = Field(description="RHEL version (e.g., rhel-9.7.z)")
    golang_version: str = Field(description="Golang version (e.g., 1.25.8)")
    cves: list[str] = Field(default_factory=list, description="CVE IDs")
    workflow: str = Field(default="brew_build", description="Workflow type (brew_build or gitlab_mr)")
    additional_jiras: list[str] = Field(default_factory=list, description="Additional Jira tickets")
    branch: str | None = Field(default=None, description="Dist-git branch")
    build_target: str | None = Field(default=None, description="Brew build target")


class GolangCVEInfo(BaseModel):
    """Information extracted from a Golang CVE Jira ticket."""

    ticket_key: str = Field(description="Jira ticket key")
    cve_ids: list[str] = Field(description="CVE IDs")
    rhel_version: str = Field(description="RHEL version (e.g., rhel-9.7.z)")
    golang_version: str = Field(default="unknown", description="Golang version")
    status: str = Field(description="Jira ticket status")
    is_zstream: bool = Field(description="Whether this is a z-stream version")
    summary: str | None = Field(default=None)
    description: str | None = Field(default=None)


class ComponentRebuildInfo(BaseModel):
    """Information about a component rebuild."""

    component: str
    ticket_key: str
    rhel_version: str
    cve_ids: list[str]
    golang_version: str

    # Repository information
    repo_url: str | None = None
    repo_path: str | None = None
    branch: str | None = None

    # Build information
    build_target: str | None = None
    scratch_task_id: str | None = None
    scratch_nvr: str | None = None
    final_task_id: str | None = None
    final_nvr: str | None = None

    # GitLab MR information (for RHEL 10+)
    fork_url: str | None = None
    mr_url: str | None = None

    # Status
    status: RebuildStatus = RebuildStatus.PENDING
    error_message: str | None = None


class BuildResult(BaseModel):
    """Result of a Brew build."""

    task_id: str
    nvr: str | None = None
    state: str | None = None
    success: bool = False
    error_message: str | None = None
    build_url: str | None = None


class RebuildSummary(BaseModel):
    """Summary of a golang rebuild operation."""

    golang_ticket: str
    components_processed: int = 0
    components_succeeded: int = 0
    components_failed: int = 0
    components_skipped: int = 0
    component_results: list[dict] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def add_result(
        self,
        component: str,
        success: bool,
        message: str = "",
        scratch_task_id: str | None = None,
        scratch_nvr: str | None = None,
    ):
        """Add a component rebuild result."""
        result = {"component": component, "success": success, "message": message}
        if scratch_task_id:
            result["scratch_task_id"] = scratch_task_id
            result["scratch_nvr"] = scratch_nvr
            result["brew_url"] = (
                f"https://brewweb.engineering.redhat.com/brew/taskinfo?taskID={scratch_task_id}"
            )
        self.component_results.append(result)
        if success:
            self.components_succeeded += 1
        else:
            self.components_failed += 1
