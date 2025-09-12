"""
Common Pydantic models shared across the BeeAI system.

This module contains common data models used across different agents
and components to ensure consistency and type safety.
"""

from typing import Optional, Dict, Any, Union
from pydantic import BaseModel, Field
from pathlib import Path
from enum import Enum


class CVEEligibilityResult(BaseModel):
    """
    Result model for CVE triage eligibility analysis.

    This model represents the outcome of analyzing whether a Jira issue
    representing a CVE should be processed by the triage agent.
    """
    is_cve: bool = Field(
        description="Whether this is a CVE (identified by SecurityTracking label)"
    )
    is_eligible_for_triage: bool = Field(
        description="Whether triage agent should process this CVE"
    )
    reason: str = Field(
        description="Explanation of the eligibility decision"
    )
    needs_internal_fix: bool | None = Field(
        default=None,
        description="True for CVEs where internal fix is needed first (only applicable for CVEs)"
    )
    error: str | None = Field(
        default=None,
        description="Error message if the issue cannot be processed"
    )


class TriageInputSchema(BaseModel):
    """Input schema for the triage agent - metadata for a JIRA issue task."""
    issue: str = Field(description="JIRA issue key (e.g., RHEL-12345)")


class Task(BaseModel):
    """A task to be processed by an agent."""
    metadata: Dict[str, Any] = Field(description="Task metadata containing issue information")
    attempts: int = Field(default=0, description="Number of processing attempts")

    def to_json(self) -> str:
        """Convert to JSON string for Redis queue storage."""
        return self.model_dump_json()

    @classmethod
    def from_issue(cls, issue: str, attempts: int = 0) -> "Task":
        """Create a task from a JIRA issue key."""
        metadata = TriageInputSchema(issue=issue)
        return cls(metadata=metadata.model_dump(), attempts=attempts)


# ============================================================================
# Rebase Agent Schemas
# ============================================================================

class RebaseInputSchema(BaseModel):
    """Input schema for the rebase agent."""
    local_clone: Path = Field(description="Path to the local clone of forked dist-git repository")
    fedora_clone: Path | None = Field(description="Path to the local clone of corresponding Fedora repository (rawhide branch), None if clone failed")
    package: str = Field(description="Package to update")
    dist_git_branch: str = Field(description="dist-git branch to update")
    version: str = Field(description="Version to update to")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    build_error: str | None = Field(description="Error encountered during package build")
    package_instructions: str | None = Field(description="Package-specific instructions for rebase")


class RebaseOutputSchema(BaseModel):
    """Output schema for the rebase agent."""
    success: bool = Field(description="Whether the rebase was successfully completed")
    status: str = Field(description="Rebase status")
    srpm_path: Path | None = Field(description="Absolute path to generated SRPM")
    files_to_git_add: list[str] | None = Field(description="List of files that should be git added and committed")
    error: str | None = Field(description="Specific details about an error")


# ============================================================================
# Backport Agent Schemas
# ============================================================================

class BackportInputSchema(BaseModel):
    """Input schema for the backport agent."""
    local_clone: Path = Field(description="Path to the local clone of forked dist-git repository")
    unpacked_sources: Path = Field(description="Path to the unpacked (using `centpkg prep`) sources")
    package: str = Field(description="Package to update")
    dist_git_branch: str = Field(description="Git branch in dist-git to be updated")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    cve_id: str = Field(default="", description="CVE ID if the jira issue is a CVE")
    build_error: str | None = Field(description="Error encountered during package build")


class BackportOutputSchema(BaseModel):
    """Output schema for the backport agent."""
    success: bool = Field(description="Whether the backport was successfully completed")
    status: str = Field(description="Backport status with details of how the potential merge conflicts were resolved")
    srpm_path: Path | None = Field(description="Absolute path to generated SRPM")
    error: str | None = Field(description="Specific details about an error")


# ============================================================================
# Triage Agent Schemas
# ============================================================================

class Resolution(Enum):
    """Triage resolution types."""
    REBASE = "rebase"
    BACKPORT = "backport"
    CLARIFICATION_NEEDED = "clarification-needed"
    NO_ACTION = "no-action"
    ERROR = "error"


class RebaseData(BaseModel):
    """Data for rebase resolution."""
    package: str = Field(description="Package name")
    version: str = Field(description="Target upstream package version (e.g., '2.4.1')")
    jira_issue: str = Field(description="Jira issue identifier")
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class BackportData(BaseModel):
    """Data for backport resolution."""
    package: str = Field(description="Package name")
    patch_url: str = Field(description="URL or reference to the source of the fix")
    justification: str = Field(description="Clear explanation of why this patch fixes the issue")
    jira_issue: str = Field(description="Jira issue identifier")
    cve_id: str = Field(description="CVE identifier")
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class ClarificationNeededData(BaseModel):
    """Data for clarification needed resolution."""
    findings: str = Field(description="Summary of the investigation")
    additional_info_needed: str = Field(description="Summary of missing information")
    jira_issue: str = Field(description="Jira issue identifier")


class NoActionData(BaseModel):
    """Data for no action resolution."""
    reasoning: str = Field(description="Reason why the issue is intentionally non-actionable")
    jira_issue: str = Field(description="Jira issue identifier")


class ErrorData(BaseModel):
    """Data for error resolution."""
    details: str = Field(description="Specific details about an error")
    jira_issue: str = Field(description="Jira issue identifier")


class TriageOutputSchema(BaseModel):
    """Output schema for the triage agent."""
    resolution: Resolution = Field(description="Triage resolution")
    data: Union[RebaseData, BackportData, ClarificationNeededData, NoActionData, ErrorData] = Field(
        description="Associated data"
    )


# ============================================================================
# Build Agent Schemas
# ============================================================================

class BuildInputSchema(BaseModel):
    """Input schema for the build agent."""
    srpm_path: Path = Field(description="Path to SRPM to build")
    dist_git_branch: str = Field(description="dist-git branch to update")
    jira_issue: str = Field(description="Jira issue to reference as resolved")


class BuildOutputSchema(BaseModel):
    """Output schema for the build agent."""
    success: bool = Field(description="Whether the build was successfully completed")
    error: str | None = Field(description="Specific details about an error")


# ============================================================================
# Log Agent Schemas
# ============================================================================

class LogInputSchema(BaseModel):
    """Input schema for the log agent."""
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    changes_summary: str = Field(description="Summary of performed changes")


class LogOutputSchema(BaseModel):
    """Output schema for the log agent."""
    title: str = Field(description="Title to use for commit message and MR")
    description: str = Field(description="Description of changes for commit message and MR")
