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
    cve_id: str | None = Field(default=None, description="CVE ID if the jira issue is a CVE")
    upstream_patches: list[str] = Field(
        description="List of URLs to upstream patches that were validated using the PatchValidator tool")
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
    version: str = Field(description="Target upstream package version to rebase to (e.g., '2.4.1')")
    jira_issue: str = Field(description="Jira issue identifier")
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class BackportData(BaseModel):
    """Data for backport resolution."""
    package: str = Field(description="Package name")
    patch_urls: list[str] = Field(
        description="A list of URLs to the sources of the fixes that were validated using the PatchValidator tool")
    justification: str = Field(description="Clear explanation of why this patch fixes the issue, linking it to the root cause")
    jira_issue: str = Field(description="Jira issue identifier")
    cve_id: str | None = Field(description="CVE identifier", default=None)
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class ClarificationNeededData(BaseModel):
    """Data for clarification needed resolution."""
    findings: str = Field(
        description="Summarize your understanding of the bug and what you investigated, "
        "e.g., \"The CVE-2025-XXXX describes a buffer overflow in the parse_input() function. "
        "I have scanned the upstream and Fedora git history for related commits but could not find a definitive fix.\""
    )
    additional_info_needed: str = Field(
        description="State what information you are missing, e.g., \"A link to the upstream commit "
        "that fixes this issue, or a patch file, is required to proceed.\""
    )
    jira_issue: str = Field(description="Jira issue identifier")


class NoActionData(BaseModel):
    """Data for no action resolution."""
    reasoning: str = Field(
        description="The reasoning why the issue is intentionally non-actionable, "
            "e.g., \"The request is for a new feature ('add dark mode') "
            "which is not appropriate for a bugfix update in RHEL.\""
    )
    jira_issue: str = Field(description="Jira issue identifier")


class ErrorData(BaseModel):
    """Data for error resolution."""
    details: str = Field(
        description="Provide specific details about the error, e.g.,"
            " \"Package 'invalid-package-name' not found "
            "in GitLab repository after examining issue details.\""
    )
    jira_issue: str = Field(description="Jira issue identifier")


class TriageOutputSchema(BaseModel):
    """Output schema for the triage agent."""
    resolution: Resolution = Field(
        description="Triage resolution, one of rebase, backport, clarification-needed, no-action, error")
    data: Union[RebaseData, BackportData, ClarificationNeededData, NoActionData, ErrorData] = Field(
        description="Associated data"
    )

    def format_for_comment(self) -> str:
        """Format the triage result in a human-readable format for Jira comments."""
        resolution = f"*Resolution*: {self.resolution.value}\n"

        match self.data:
            case BackportData():
                fix_version_text = f"\n*Fix Version*: {self.data.fix_version}" if self.data.fix_version else ""

                patch_urls_text = "\n".join([f"*Patch URL {i+1}*: {url}" for i, url in enumerate(self.data.patch_urls)])
                return (
                    f"{resolution}"
                    f"{patch_urls_text}\n"
                    f"*Justification*: {self.data.justification}"
                    f"{fix_version_text}"
                )

            case RebaseData():
                fix_version_text = f"\n*Fix Version*: {self.data.fix_version}" if self.data.fix_version else ""

                return (
                    f"{resolution}"
                    f"*Package*: {self.data.package}\n"
                    f"*Version*: {self.data.version}{fix_version_text}"
                )

            case ClarificationNeededData():
                return (
                    f"{resolution}"
                    f"*Findings*: {self.data.findings}\n"
                    f"*Additional info needed*: {self.data.additional_info_needed}"
                )

            case NoActionData():
                return (
                    f"{resolution}"
                    f"*Reasoning*: {self.data.reasoning}"
                )

            case ErrorData():
                return (
                    f"{resolution}"
                    f"*Details*: {self.data.details}"
                )

            case _:
                # Fallback to JSON format
                return self.model_dump_json(indent=4)


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
    is_timeout: bool = Field(default=False, description="Whether the build failed due to a timeout")


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


# ============================================================================
# Merge Request Metadata Cache Schema
# ============================================================================

class CachedMRMetadata(BaseModel):
    """Cached merge request metadata for reuse across streams."""
    operation_type: str = Field(description="Type of operation (backport or rebase)")
    title: str = Field(description="Merge request title")
    package: str = Field(description="Package name")
    details: str = Field(description="Operation-specific identifier (list of upstream patch URLs for backport, version for rebase)")


# ============================================================================
# GitLab Tools Schemas
# ============================================================================

class FailedPipelineJob(BaseModel):
    """Represents a failed job in a GitLab pipeline."""

    id: str = Field(description="Pipeline job ID as a string")
    name: str = Field(description="Name of the job")
    url: str = Field(description="Full URL to the job in GitLab")
    status: str = Field(description="Job status")
    stage: str = Field(description="Pipeline stage the job belongs to")
    artifacts_url: str = Field(
        description="URL to browse job artifacts, empty string if no artifacts"
    )


class CommentReply(BaseModel):
    """Represents a reply comment in a discussion thread."""

    author: str = Field(description="Username of the reply author")
    message: str = Field(description="The reply message text")
    created_at: str = Field(
        description="ISO 8601 timestamp when reply was created"
    )


class MergeRequestComment(BaseModel):
    """Represents a comment from a GitLab merge request by an authorized member."""

    author: str = Field(description="Username of the comment author")
    message: str = Field(description="The comment message text")
    created_at: str = Field(
        description="ISO 8601 timestamp when comment was created"
    )
    file_path: str = Field(
        default="",
        description="File path if comment targets specific code, "
        "empty for general comments"
    )
    line_number: int | None = Field(
        default=None,
        description="Line number in the current state of the file. "
        "WARNING: If subsequent commits modified the file after this comment "
        "was made, this line number may differ from where the comment was "
        "originally placed. None for general comments."
    )
    line_type: str = Field(
        default="",
        description="Type of line in the diff: 'new' (added line), "
        "'old' (removed line), 'unchanged' (context line), "
        "or empty for general comments"
    )
    discussion_id: str = Field(
        default="",
        description="Discussion/thread ID this comment belongs to"
    )
    replies: list[CommentReply] = Field(
        default_factory=list,
        description="List of replies to this comment in the thread, "
        "ordered chronologically"
    )