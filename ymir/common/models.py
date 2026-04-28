"""
Common Pydantic models shared across the BeeAI system.

This module contains common data models used across different agents
and components to ensure consistency and type safety.
"""

from datetime import datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, RootModel


class TriageEligibility(StrEnum):
    """Three-state triage eligibility."""

    IMMEDIATELY = "immediately"
    PENDING_DEPENDENCIES = "pending-dependencies"
    NEVER = "never"


class CVEEligibilityResult(BaseModel):
    """
    Result model for CVE triage eligibility analysis.

    This model represents the outcome of analyzing whether a Jira issue
    representing a CVE should be processed by the triage agent.
    """

    is_cve: bool = Field(description="Whether this is a CVE (identified by SecurityTracking label)")
    eligibility: TriageEligibility = Field(description="When triage agent should process this CVE")
    reason: str = Field(description="Explanation of the eligibility decision")
    needs_internal_fix: bool | None = Field(
        default=None,
        description="True for CVEs where internal fix is needed first (only applicable for CVEs)",
    )
    error: str | None = Field(default=None, description="Error message if the issue cannot be processed")
    pending_zstream_issues: list[str] | None = Field(
        default=None,
        description="Jira issue keys of unshipped Z-stream clones; at least one must ship before triage",
    )

    @property
    def is_eligible_for_triage(self) -> bool:
        return self.eligibility == TriageEligibility.IMMEDIATELY


class TriageInputSchema(BaseModel):
    """Input schema for the triage agent - metadata for a JIRA issue task."""

    issue: str = Field(description="JIRA issue key (e.g., RHEL-12345)")
    force_cve_triage: bool = Field(
        default=False,
        description=(
            "Force triage of CVE issues that would normally be deferred or rejected"
            " (eligibility=PENDING_DEPENDENCIES or NEVER)"
        ),
    )
    is_older_zstream: bool = Field(
        default=False,
        description="Whether the issue targets an older Z-stream",
    )


class Task(BaseModel):
    """A task to be processed by an agent."""

    metadata: dict[str, Any] = Field(description="Task metadata containing issue information")
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
    fedora_clone: Path | None = Field(
        description="Path to the local clone of corresponding Fedora repository "
        "(rawhide branch), None if clone failed"
    )
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
    files_to_git_add: list[str] | None = Field(
        description="List of files that should be git added and committed"
    )
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
        description="List of URLs to upstream patches that were validated using the get_patch_from_url tool"
    )
    build_error: str | None = Field(description="Error encountered during package build")
    pkg_tool: str = Field(default="centpkg", description="Package tool command with arguments")


class BackportOutputSchema(BaseModel):
    """Output schema for the backport agent."""

    success: bool = Field(description="Whether the backport was successfully completed")
    status: str = Field(
        description="Backport status with details of how the potential merge conflicts were resolved"
    )
    srpm_path: Path | None = Field(description="Absolute path to generated SRPM")
    error: str | None = Field(description="Specific details about an error")


class RebuildOutputSchema(BaseModel):
    """Output schema for the rebuild agent."""

    success: bool = Field(description="Whether the rebuild was successfully completed")
    merge_request_url: str | None = Field(default=None, description="URL of the opened merge request")
    error: str | None = Field(default=None, description="Specific details about an error")


# ============================================================================
# Triage Agent Schemas
# ============================================================================


class Resolution(Enum):
    """Triage resolution types."""

    REBASE = "rebase"
    BACKPORT = "backport"
    REBUILD = "rebuild"
    CLARIFICATION_NEEDED = "clarification-needed"
    OPEN_ENDED_ANALYSIS = "open-ended-analysis"
    POSTPONED = "postponed"
    NOT_AFFECTED = "not-affected"
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
        description="A list of URLs to the sources of the fixes "
        "that were validated using the get_patch_from_url tool"
    )
    justification: str = Field(
        description="Clear explanation of why this patch fixes the issue, linking it to the root cause"
    )
    jira_issue: str = Field(description="Jira issue identifier")
    cve_id: str | None = Field(description="CVE identifier", default=None)
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class RebuildData(BaseModel):
    """Data for rebuild resolution."""

    package: str = Field(description="Package name")
    jira_issue: str = Field(description="Jira issue identifier")
    cve_id: str | None = Field(description="CVE identifier", default=None)
    dependency_issue: str | None = Field(
        description="Key of the dependency Jira issue that triggered the rebuild",
        default=None,
    )
    dependency_component: str | None = Field(
        description="Name of the dependency component that triggered the rebuild (e.g., 'golang', 'openssl')",
        default=None,
    )
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class ClarificationNeededData(BaseModel):
    """Data for clarification needed resolution."""

    findings: str = Field(
        description="Summarize your understanding of the bug and what you investigated, "
        'e.g., "The CVE-2025-XXXX describes a buffer overflow in the parse_input() function. '
        "I have scanned the upstream and Fedora git history for related "
        'commits but could not find a definitive fix."'
    )
    additional_info_needed: str = Field(
        description='State what information you are missing, e.g., "A link to the upstream commit '
        'that fixes this issue, or a patch file, is required to proceed."'
    )
    jira_issue: str = Field(description="Jira issue identifier")


class OpenEndedAnalysisData(BaseModel):
    """Data for open-ended analysis resolution."""

    summary: str = Field(
        description="Concise summary (2-3 sentences) of the issue analysis and findings. "
        "Focus on what the issue is and why it can't be resolved as a simple rebase, backport, or rebuild. "
        'e.g., "The issue requests updating BuildRequires for package-x to version >= 2.0 '
        'due to a new API used in the latest release."'
    )
    recommendation: str = Field(
        description="Concise recommended course of action (1-2 sentences). "
        'e.g., "This issue requires a specfile adjustment to update BuildRequires '
        'for package-x to version >= 2.0. No upstream source changes needed." '
        'or "No action needed — this is a duplicate of RHEL-12345."'
    )
    jira_issue: str = Field(description="Jira issue identifier")


class PostponedData(BaseModel):
    """Data for postponed resolution (waiting on dependencies to ship)."""

    summary: str = Field(description="Reason for postponement")
    pending_issues: list[str] = Field(description="Jira issue keys of dependencies not yet shipped")
    jira_issue: str = Field(description="Jira issue identifier")
    package: str | None = Field(default=None, description="Package name (for rebuild postponements)")
    fix_version: str | None = Field(
        default=None, description="Fix version in Jira (for rebuild postponements)"
    )
    cve_id: str | None = Field(default=None, description="CVE identifier (for rebuild postponements)")
    dependency_issue: str | None = Field(
        default=None,
        description="Key of the dependency Jira issue that triggered the rebuild (for rebuild postponements)",
    )
    dependency_component: str | None = Field(
        default=None,
        description="Dependency component name (for rebuild postponements)",
    )


class NotAffectedData(BaseModel):
    """Data for not-affected resolution (CVE does not apply to this package)."""

    justification_category: str | None = Field(
        description="Red Hat justification category, e.g. 'Vulnerable Code not Present'",
        default=None,
    )
    explanation: str = Field(description="Detailed explanation of why the CVE does not affect this package")
    jira_issue: str = Field(description="Jira issue identifier")


class ApplicabilityResult(BaseModel):
    """Output schema for the CVE applicability check agent."""

    is_affected: bool = Field(description="True if affected or inconclusive, False if clearly not affected")
    justification_category: str | None = Field(
        description="Red Hat justification category when not affected, None if affected",
        default=None,
    )
    explanation: str = Field(description="Detailed reasoning for the determination")


class ErrorData(BaseModel):
    """Data for error resolution."""

    details: str = Field(
        description="Provide specific details about the error, e.g.,"
        " \"Package 'invalid-package-name' not found "
        'in GitLab repository after examining issue details."'
    )
    jira_issue: str = Field(description="Jira issue identifier")


TRIAGE_DISCLAIMER = (
    "\n\n_By following Ymir suggestions, you agree to comply with the "
    "[Guidelines on Use of AI Generated Content"
    "|https://source.redhat.com/departments/legal/legal_compliance_ethics/"
    "compliance_folder/appendix_1_to_policy_on_the_use_of_ai_technologypdf] "
    "and [Guidelines for Responsible Use of AI Code Assistants"
    "|https://source.redhat.com/projects_and_programs/ai/wiki/"
    "code_assistants_guidelines_for_responsible_use_of_ai_code_assistants]._"
)

AUTOMATED_RESOLUTION_NOT_SUPPORTED = (
    "\n\n_Note: Automated resolution for this resolution type "
    "is not yet supported by Ymir. Manual action is required._"
)


class TriageOutputSchema(BaseModel):
    """Output schema for the triage agent."""

    resolution: Resolution = Field(
        description="Triage resolution, one of rebase, backport, rebuild, "
        "clarification-needed, open-ended-analysis, postponed, error"
    )
    data: (
        RebaseData
        | BackportData
        | RebuildData
        | ClarificationNeededData
        | OpenEndedAnalysisData
        | PostponedData
        | NotAffectedData
        | ErrorData
    ) = Field(description="Associated data")

    def format_for_comment(self, auto_chain: bool = False) -> str:
        """Format the triage result in a human-readable format for Jira comments."""
        resolution = f"*Resolution*: {self.resolution.value}\n"
        follow_up_note = (
            ""
            if auto_chain
            else (
                "\n\n_Automated individual follow-up workflow for this "
                "resolution type is planned for Q2 2026. Stay tuned._"
            )
        )

        match self.data:
            case BackportData():
                fix_version_text = (
                    f"\n*Fix Version*: {self.data.fix_version}" if self.data.fix_version else ""
                )

                patch_urls_text = "\n".join(
                    [f"*Patch URL {i + 1}*: {url}" for i, url in enumerate(self.data.patch_urls)]
                )
                return (
                    f"{resolution}"
                    f"{patch_urls_text}\n"
                    f"*Justification*: {self.data.justification}"
                    f"{fix_version_text}"
                    f"{follow_up_note}"
                    f"{TRIAGE_DISCLAIMER}"
                )

            case RebaseData():
                fix_version_text = (
                    f"\n*Fix Version*: {self.data.fix_version}" if self.data.fix_version else ""
                )

                return (
                    f"{resolution}"
                    f"*Package*: {self.data.package}\n"
                    f"*Version*: {self.data.version}{fix_version_text}"
                    f"{follow_up_note}"
                    f"{TRIAGE_DISCLAIMER}"
                )

            case RebuildData():
                fix_version_text = (
                    f"\n*Fix Version*: {self.data.fix_version}" if self.data.fix_version else ""
                )
                dep_text = (
                    f"\n*Dependency Issue*: {self.data.dependency_issue}"
                    if self.data.dependency_issue
                    else ""
                )
                dep_comp_text = (
                    f"\n*Dependency Component*: {self.data.dependency_component}"
                    if self.data.dependency_component
                    else ""
                )

                return (
                    f"{resolution}"
                    f"*Package*: {self.data.package}"
                    f"{dep_comp_text}"
                    f"{dep_text}"
                    f"{fix_version_text}"
                    f"{follow_up_note}"
                    f"{TRIAGE_DISCLAIMER}"
                )

            case ClarificationNeededData():
                return (
                    f"{resolution}"
                    f"*Findings*: {self.data.findings}\n"
                    f"*Additional info needed*: {self.data.additional_info_needed}"
                    f"{TRIAGE_DISCLAIMER}"
                )

            case OpenEndedAnalysisData():
                return (
                    f"*Summary*: {self.data.summary}\n"
                    f"*Recommendation*: {self.data.recommendation}"
                    f"{AUTOMATED_RESOLUTION_NOT_SUPPORTED}"
                    f"{TRIAGE_DISCLAIMER}"
                )

            case PostponedData():
                pending_text = "\n".join(f"* {key}" for key in self.data.pending_issues)
                if len(self.data.pending_issues) == 1:
                    heading = "*Waiting for*:"
                else:
                    heading = "*Waiting for at least one of*:"
                return (
                    f"{resolution}"
                    f"*Summary*: {self.data.summary}\n"
                    f"{heading}\n{pending_text}"
                    f"{TRIAGE_DISCLAIMER}"
                )

            case NotAffectedData():
                category = self.data.justification_category or "Not Affected"
                return (
                    f"*Recommendation: Not a Bug / {category}*\n\n{self.data.explanation}{TRIAGE_DISCLAIMER}"
                )

            case ErrorData():
                return f"{resolution}*Details*: {self.data.details}{TRIAGE_DISCLAIMER}"

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
# Merge Request Agent Schemas
# ============================================================================


class MergeRequestInputSchema(BaseModel):
    """Input schema for the merge request agent."""

    local_clone: Path = Field(description="Path to the local clone of forked dist-git repository")
    package: str = Field(description="Package to update")
    dist_git_branch: str = Field(description="dist-git branch to update")
    jira_issue: str = Field(description="Jira issue identifier")
    merge_request_url: str = Field(description="URL of the merge request")
    merge_request_title: str = Field(description="Title of the MR")
    merge_request_description: str = Field(description="Description of the MR")
    comments: str = Field(description="List of MR comments as a JSON string, including schema")
    fedora_clone: Path | None = Field(
        description=(
            "Path to the local clone of corresponding Fedora repository (rawhide branch), "
            "None if clone failed"
        ),
    )
    build_error: str | None = Field(description="Error encountered during package build")


class MergeRequestOutputSchema(BaseModel):
    success: bool = Field(description="Whether the MR update was successfully completed")
    status: str = Field(
        description="MR update status with details of changes performed, in a form of a commit message"
    )
    srpm_path: Path | None = Field(description="Absolute path to generated SRPM")
    files_to_git_add: list[str] | None = Field(
        description="List of files that should be git added and committed"
    )
    error: str | None = Field(description="Specific details about an error")


# ============================================================================
# Merge Request Metadata Cache Schema
# ============================================================================


class CachedMRMetadata(BaseModel):
    """Cached merge request metadata for reuse across streams."""

    operation_type: str = Field(description="Type of operation (backport or rebase)")
    title: str = Field(description="Merge request title")
    package: str = Field(description="Package name")
    details: str = Field(
        description="Operation-specific identifier "
        "(list of upstream patch URLs for backport, version for rebase)"
    )


# ============================================================================
# GitLab Tools Schemas
# ============================================================================


class OpenMergeRequestResult(BaseModel):
    """Result of opening a merge request."""

    url: str = Field(description="URL of the merge request")
    is_new_mr: bool = Field(description="True if newly created, False if an existing MR was reused")


class FailedPipelineJob(BaseModel):
    """Represents a failed job in a GitLab pipeline."""

    id: str = Field(description="Pipeline job ID as a string")
    name: str = Field(description="Name of the job")
    url: str = Field(description="Full URL to the job in GitLab")
    status: str = Field(description="Job status")
    stage: str = Field(description="Pipeline stage the job belongs to")
    artifacts_url: str = Field(description="URL to browse job artifacts, empty string if no artifacts")


class CommentReply(BaseModel):
    """Represents a reply comment in a discussion thread."""

    author: str | None = Field(description="Username of the reply author")
    message: str | None = Field(description="The reply message text")
    created_at: datetime | None = Field(description="Timestamp when reply was created")


class MergeRequestComment(BaseModel):
    """Represents a comment from a GitLab merge request by an authorized member."""

    author: str | None = Field(description="Username of the comment author")
    message: str | None = Field(description="The comment message text")
    created_at: datetime | None = Field(description="Timestamp when comment was created")
    file_path: str = Field(
        default="",
        description="File path if comment targets specific code, empty for general comments",
    )
    line_number: int | None = Field(
        default=None,
        description="Line number in the current state of the file. "
        "WARNING: If subsequent commits modified the file after this comment "
        "was made, this line number may differ from where the comment was "
        "originally placed. None for general comments.",
    )
    line_type: str = Field(
        default="",
        description="Type of line in the diff: 'new' (added line), "
        "'old' (removed line), 'unchanged' (context line), "
        "or empty for general comments",
    )
    discussion_id: str = Field(default="", description="Discussion/thread ID this comment belongs to")
    replies: list[CommentReply] = Field(
        default_factory=list,
        description="List of replies to this comment in the thread, ordered chronologically",
    )


MergeRequestComments = RootModel[list[MergeRequestComment]]


class MergeRequestDetails(BaseModel):
    source_repo: str = Field(description="Clonable git URL of source project of the MR (fork)")
    source_branch: str = Field(description="Source branch of the MR")
    target_repo_name: str = Field(description="Name of the target repository (package name)")
    target_branch: str = Field(description="Target branch of the MR")
    title: str = Field(description="Title of the MR")
    description: str = Field(description="Description of the MR")
    last_updated_at: datetime = Field(description="Timestamp of the last update (push)")
    comments: MergeRequestComments = Field(description="List of relevant MR comments")
