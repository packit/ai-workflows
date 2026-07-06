"""
Common Pydantic models shared across the BeeAI system.

This module contains common data models used across different agents
and components to ensure consistency and type safety.
"""

import re
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Literal

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
    needs_internal_fix: bool = Field(
        default=False,
        description="Whether the CVE needs an internal RHEL fix (SecurityTracking label, Z-stream)",
    )
    internal_target_branch: str | None = Field(
        default=None,
        description="Pre-computed internal RHEL branch to inspect (e.g. 'rhel-10.2') "
        "when needs_internal_fix is true",
    )


class Task(BaseModel):
    """A task to be processed by an agent."""

    metadata: dict[str, Any] = Field(description="Task metadata containing issue information")
    attempts: int = Field(default=0, description="Number of processing attempts")
    user_triggered: bool = Field(
        default=False,
        description="True when a maintainer triggered this run via the ymir_todo label — "
        "causes agents to post comments and intermediate failure labels that are "
        "otherwise suppressed (default is silent).",
    )

    def to_json(self) -> str:
        """Convert to JSON string for Redis queue storage."""
        return self.model_dump_json()

    @classmethod
    def from_issue(cls, issue: str, attempts: int = 0, user_triggered: bool = False) -> "Task":
        """Create a task from a JIRA issue key."""
        metadata = TriageInputSchema(issue=issue)
        return cls(metadata=metadata.model_dump(), attempts=attempts, user_triggered=user_triggered)


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
    triage_summary: str | None = Field(
        default=None,
        description="Triage context: what was investigated and guidance on how the rebase should be done",
    )


class RebaseOutputSchema(BaseModel):
    """Output schema for the rebase agent."""

    success: bool = Field(description="Whether the rebase was successfully completed")
    status: str = Field(description="Rebase status")
    srpm_path: Path | None = Field(description="Absolute path to generated SRPM")
    files_to_git_add: list[str] | None = Field(
        description="List of files that should be git added and committed"
    )
    error: str | None = Field(description="Specific details about an error")
    abandon_autorelease: bool = Field(
        default=False,
        description="Set to true if maintainer rules indicate that %autorelease should not be used "
        "for Z-stream releases and a numeric release counter should be used instead",
    )


# ============================================================================
# Backport Agent Schemas
# ============================================================================


class BackportInputSchema(BaseModel):
    """Input schema for the backport agent."""

    local_clone: Path = Field(description="Path to the local clone of forked dist-git repository")
    unpacked_sources: Path = Field(description="Path to the unpacked sources")
    package: str = Field(description="Package to update")
    dist_git_branch: str = Field(description="Git branch in dist-git to be updated")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    cve_id: str | None = Field(
        default=None,
        description="CVE ID(s) if the jira issue is a CVE; may contain multiple CVE IDs",
    )
    upstream_patches: list[str] = Field(
        description="List of URLs to upstream patches that were validated using the get_patch_from_url tool"
    )
    build_error: str | None = Field(description="Error encountered during package build")
    triage_summary: str | None = Field(
        default=None,
        description="Triage context: what was investigated and guidance on how the backport should be done",
    )
    has_extract_log_snippets: bool = Field(
        default=False,
        description="Whether the extract_log_snippets tool is available",
    )


class BackportOutputSchema(BaseModel):
    """Output schema for the backport agent."""

    success: bool = Field(description="Whether the backport was successfully completed")
    status: str = Field(
        description="Backport status with details of how the potential merge conflicts were resolved"
    )
    srpm_path: Path | None = Field(description="Absolute path to generated SRPM")
    error: str | None = Field(description="Specific details about an error")
    abandon_autorelease: bool = Field(
        default=False,
        description="Set to true if maintainer rules indicate that %autorelease should not be used "
        "for Z-stream releases and a numeric release counter should be used instead",
    )


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
    justification: str | None = Field(
        default=None,
        description="Reviewer-facing rationale: why this version fixes the issue. "
        "Do NOT include investigation narrative here.",
    )
    triage_summary: str | None = Field(
        default=None,
        description="Investigation log and downstream-agent handoff: what was searched, "
        "what was ruled out, caveats, and operational guidance. "
        "Do NOT repeat the justification rationale here.",
    )
    jira_issue: str = Field(description="Jira issue identifier")
    cve_id: str | None = Field(
        description="CVE identifier(s); include ALL CVE IDs when the issue covers multiple CVEs",
        default=None,
    )
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class BackportData(BaseModel):
    """Data for backport resolution."""

    package: str = Field(description="Package name")
    patch_urls: list[str] = Field(
        description="A list of URLs to the sources of the fixes, each validated "
        "using the get_patch_from_url tool. Prefer a single GitHub PR / GitLab MR "
        "URL when all commits originate from one PR/MR (e.g. "
        "'https://github.com/org/repo/pull/42.patch'). Use individual commit URLs only "
        "when commits come from different PRs or are committed directly."
    )
    justification: str = Field(
        description=(
            "Reviewer-facing rationale: why this patch fixes the issue, linking it to the root cause. "
            "For CVE issues: explicitly state whether the patch mentions the CVE ID, and if not, "
            "explain how the patch addresses the specific vulnerability. "
            "Do NOT include investigation narrative here."
        )
    )
    triage_summary: str | None = Field(
        default=None,
        description="Investigation log and downstream-agent handoff: what was searched, "
        "what was ruled out, caveats, and operational guidance "
        "(e.g. which part of a broad patch is the actual fix). "
        "Do NOT repeat the justification rationale here.",
    )
    jira_issue: str = Field(description="Jira issue identifier")
    cve_id: str | None = Field(
        description="CVE identifier(s); include ALL CVE IDs when the issue covers multiple CVEs",
        default=None,
    )
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)


class ConsolidatedIssue(BaseModel):
    """A sibling Jira issue consolidated into the same rebuild task."""

    issue_key: str = Field(description="Jira issue key (e.g. RHEL-67890)")
    dependency_issue: str | None = Field(
        description="Key of the dependency Jira issue (e.g. RHEL-12345)",
        default=None,
    )
    dependency_component: str | None = Field(
        description="Component name of the dependency (e.g. 'golang')",
        default=None,
    )


class RebuildData(BaseModel):
    """Data for rebuild resolution."""

    package: str = Field(description="Package name")
    jira_issue: str = Field(description="Jira issue identifier")
    cve_id: str | None = Field(
        description="CVE identifier(s); include ALL CVE IDs when the issue covers multiple CVEs",
        default=None,
    )
    justification: str | None = Field(
        default=None,
        description="Reviewer-facing rationale: why a rebuild is needed and how it addresses the issue. "
        "Do NOT include investigation narrative here.",
    )
    triage_summary: str | None = Field(
        default=None,
        description="Investigation log and downstream-agent handoff: what was searched, "
        "what was ruled out, caveats, and operational guidance. "
        "Do NOT repeat the justification rationale here.",
    )
    dependency_issue: str | None = Field(
        description="Key of the dependency Jira issue that triggered the rebuild",
        default=None,
    )
    dependency_component: str | None = Field(
        description="Name of the dependency component that triggered the rebuild (e.g., 'golang', 'openssl')",
        default=None,
    )
    fix_version: str | None = Field(description="Fix version in Jira (e.g., 'rhel-9.8')", default=None)
    consolidated_issues: list[ConsolidatedIssue] = Field(
        default_factory=list,
        description="Sibling issues consolidated into this rebuild task",
    )
    consolidation_summary: str | None = Field(
        default=None,
        description="Summary of sibling consolidation analysis",
    )

    @property
    def all_jira_issues(self) -> list[str]:
        """Return the primary issue plus all consolidated sibling issue keys."""
        return [self.jira_issue] + [ci.issue_key for ci in self.consolidated_issues]


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
    cve_id: str | None = Field(
        description="CVE identifier(s); include ALL CVE IDs when the issue covers multiple CVEs",
        default=None,
    )
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
                triage_summary_text = (
                    f"\n*Triage Reasoning*: {self.data.triage_summary.strip()}"
                    if self.data.triage_summary and self.data.triage_summary.strip()
                    else ""
                )
                return (
                    f"{resolution}"
                    f"{patch_urls_text}\n"
                    f"*Justification*: {self.data.justification}"
                    f"{triage_summary_text}"
                    f"{fix_version_text}"
                    f"{follow_up_note}"
                    f"{TRIAGE_DISCLAIMER}"
                )

            case RebaseData():
                fix_version_text = (
                    f"\n*Fix Version*: {self.data.fix_version}" if self.data.fix_version else ""
                )
                justification_text = (
                    f"\n*Justification*: {self.data.justification}" if self.data.justification else ""
                )
                triage_summary_text = (
                    f"\n*Triage Reasoning*: {self.data.triage_summary.strip()}"
                    if self.data.triage_summary and self.data.triage_summary.strip()
                    else ""
                )

                return (
                    f"{resolution}"
                    f"*Package*: {self.data.package}\n"
                    f"*Version*: {self.data.version}"
                    f"{justification_text}{triage_summary_text}{fix_version_text}"
                    f"{follow_up_note}"
                    f"{TRIAGE_DISCLAIMER}"
                )

            case RebuildData():
                fix_version_text = (
                    f"\n*Fix Version*: {self.data.fix_version}" if self.data.fix_version else ""
                )
                justification_text = (
                    f"\n*Justification*: {self.data.justification}" if self.data.justification else ""
                )
                triage_summary_text = (
                    f"\n*Triage Reasoning*: {self.data.triage_summary.strip()}"
                    if self.data.triage_summary and self.data.triage_summary.strip()
                    else ""
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

                consolidation_text = ""
                if self.data.consolidation_summary:
                    consolidation_text = (
                        f"\n\n*Sibling consolidation analysis:*\n{self.data.consolidation_summary}"
                    )

                return (
                    f"{resolution}"
                    f"*Package*: {self.data.package}"
                    f"{justification_text}"
                    f"{triage_summary_text}"
                    f"{dep_comp_text}"
                    f"{dep_text}"
                    f"{fix_version_text}"
                    f"{consolidation_text}"
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
                vex_guide = (
                    "\n\n_See [VEX Not Affected Justifications|"
                    "https://redhat.atlassian.net/wiki/spaces/PRODSEC/pages/289223326]"
                    " for justification category definitions._"
                )
                return (
                    f"*Recommendation: Not a Bug / {category}*\n\n"
                    f"{self.data.explanation}{vex_guide}{TRIAGE_DISCLAIMER}"
                )

            case ErrorData():
                return f"{resolution}*Details*: {self.data.details}{TRIAGE_DISCLAIMER}"

            case _:
                # Fallback to JSON format
                return self.model_dump_json(indent=4)


# ============================================================================
# Build Agent Schemas
# ============================================================================


class BuildInstructionsInput(BaseModel):
    """Input schema for the build agent instructions template."""

    has_extract_log_snippets: bool = Field(
        default=False,
        description="Whether the extract_log_snippets tool is available",
    )


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
    is_infra_error: bool = Field(
        default=False,
        description="Whether the failure was caused by a Copr API or infrastructure error, not a build error",
    )


# ============================================================================
# Log Agent Schemas
# ============================================================================


class LogInputSchema(BaseModel):
    """Input schema for the log agent."""

    jira_issue: str = Field(description="Jira issue to reference as resolved")
    changes_summary: str = Field(description="Summary of performed changes")
    source_changelog: str | None = Field(
        default=None,
        description="Changelog message from the source commit to reuse, if available",
    )


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
    status: str = Field(description="Job status (e.g. failed, canceled)")
    stage: str = Field(description="Pipeline stage the job belongs to")
    artifacts_url: str = Field(description="URL to browse job artifacts, empty string if no artifacts")
    allow_failure: bool = Field(
        default=False,
        description="True if the job is allowed to fail (allow_failure: true in .gitlab-ci.yml). "
        "A job with allow_failure=true does not block the pipeline even when it fails.",
    )


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


# ============================================================================
# Supervisor / Issue Verification Types
# ============================================================================


class IssueStatus(StrEnum):
    NEW = "New"
    PLANNING = "Planning"  # RHEL only
    REFINEMENT = "Refinement"  # RHELMISC only
    IN_PROGRESS = "In Progress"
    INTEGRATION = "Integration"  # RHEL only
    RELEASE_PENDING = "Release Pending"  # RHEL only
    DONE = "Done"  # RHEL ONLY
    CLOSED = "Closed"


class TestCoverage(StrEnum):
    MANUAL = "Manual"
    AUTOMATED = "Automated"
    REGRESSION_ONLY = "RegressionOnly"
    NEW_TEST_COVERAGE = "New Test Coverage"


class PreliminaryTesting(StrEnum):
    REQUESTED = "Requested"
    FAIL = "Fail"
    PASS = "Pass"  # noqa: S105
    READY = "Ready"


class ErrataStatus(StrEnum):
    NEW_FILES = "NEW_FILES"
    QE = "QE"
    REL_PREP = "REL_PREP"
    PUSH_READY = "PUSH_READY"
    IN_PUSH = "IN_PUSH"
    DROPPED_NO_SHIP = "DROPPED_NO_SHIP"
    SHIPPED_LIVE = "SHIPPED_LIVE"


class ErrataComment(BaseModel):
    authorName: str
    authorEmail: str | None
    created: datetime
    body: str


class Erratum(BaseModel):
    id: int
    full_advisory: str
    url: str
    synopsis: str
    status: ErrataStatus
    jira_issues: list[str]
    release_id: int
    publish_date: datetime | None
    last_status_transition_timestamp: datetime
    assigned_to_email: str
    package_owner_email: str


class FullErratum(Erratum):
    comments: list[ErrataComment] | None = None


class GitlabMergeRequestState(StrEnum):
    OPEN = "opened"
    CLOSED = "closed"
    MERGED = "merged"


class GitlabMergeRequest(BaseModel):
    project: str
    iid: int
    url: str
    title: str
    description: str
    state: GitlabMergeRequestState
    merged_at: datetime | None


class Issue(BaseModel):
    """A representation of a JIRA issue, with fields that we care about for RHEL development.

    RHEL development occurs in two JIRA projects - RHELMISC and RHEL, while many fields
    are standard in JIRA or common to both, some fields will only be populated for RHEL issues.

    Defects and enhancements are covered in the RHEL project, the RHELMISC project is used for
    tracking related activities of various types; we'll use issues in RHELMISC to tag Errata for
    human attention.
    """

    key: str
    url: str
    assigned_team: str | None = None
    summary: str
    components: list[str]
    status: IssueStatus
    labels: list[str]
    fix_versions: list[str]
    errata_link: str | None  # RHEL only
    fixed_in_build: str | None = None  # RHEL only
    test_coverage: list[TestCoverage] | None = None  # RHEL only
    preliminary_testing: PreliminaryTesting | None = None  # RHEL only


class JiraComment(ErrataComment):
    id: str


class FullIssue(Issue):
    description: str
    comments: list[JiraComment]


class TestingFarmRequestState(StrEnum):
    NEW = "new"
    QUEUED = "queued"
    RUNNING = "running"
    ERROR = "error"
    CANCELED = "canceled"
    CANCEL_REQUESTED = "cancel-requested"
    COMPLETE = "complete"


class TestingFarmRequestResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"
    ERROR = "error"


class TestingFarmRequest(BaseModel):
    id: str
    url: str
    state: TestingFarmRequestState
    result: TestingFarmRequestResult = TestingFarmRequestResult.UNKNOWN
    error_reason: str | None = None
    result_xunit_url: str | None = None
    created: datetime
    updated: datetime

    # We save the raw data to use during test reproduction
    test_data: dict[str, Any]
    environments_data: list[dict[str, Any]]

    @property
    def arches(self) -> list[str]:
        return [env["arch"] for env in self.environments_data]

    @property
    def build_nvr(self) -> str:
        versions = {
            variables["BUILDS"]
            for env in self.environments_data
            if (variables := env.get("variables")) and "BUILDS" in variables
        }
        if len(versions) == 1:
            return versions.pop()

        artifacts = {
            id
            for env in self.environments_data
            for artifact in env.get("artifacts", [])
            if artifact.get("type") == "redhat-brew-build" and (id := artifact.get("id"))
        }
        if len(artifacts) == 1:
            return artifacts.pop()

        raise ValueError("Can't determine package version for request")


class YmirTag(BaseModel):
    """A magic string appearing in the description of an issue that
    associates it with a particular resource - like an erratum.

    This method of labelling issues and the format is borrowed from NEWA.
    Using a custom field would be cleaner.
    """

    type: Literal["needs_attention"]
    resource: Literal["erratum"]
    id: str

    _LEGACY_PREFIXES = ("JOTNAR",)

    def __str__(self) -> str:
        return f"::: YMIR {self.type} E: {self.id.strip()} :::"

    def all_formats(self) -> list[str]:
        """Current and legacy tag strings for backwards-compatible search."""
        return [str(self)] + [f"::: {p} {self.type} E: {self.id.strip()} :::" for p in self._LEGACY_PREFIXES]


class TestingState(StrEnum):
    NOT_RUNNING = "tests-not-running"
    PENDING = "tests-pending"
    RUNNING = "tests-running"
    ERROR = "tests-error"
    FAILED = "tests-failed"
    PASSED = "tests-passed"
    WAIVED = "tests-waived"


# ============================================================================
# Errata Workflow Models
# ============================================================================


class ErratumPackageFileList(RootModel):
    """Map variant and architecture to a set of subpackage names shipped for that architecture.

    Example::

        {
            "AppStream": {
                "SRPMS": {"libtiff"},
                "aarch64": {"libtiff", "libtiff-devel", ...}
            }
        }
    """

    root: dict[str, dict[str, set[str]]]


class ErratumBuild(BaseModel):
    """A single erratum build: NVR + package file list."""

    nvr: str
    package_file_list: ErratumPackageFileList


class ErratumBuildMap(RootModel):
    """Map package name to ErratumBuild."""

    root: dict[str, ErratumBuild]


class TransitionRuleOutcome(StrEnum):
    BLOCK = "BLOCK"
    OK = "OK"
    UNKNOWN = "UNKNOWN"


class TransitionRule(BaseModel):
    name: str
    outcome: TransitionRuleOutcome
    details: str


class TransitionRuleSet(BaseModel):
    from_status: ErrataStatus
    to_status: ErrataStatus
    rules: list[TransitionRule]

    @property
    def all_ok(self) -> bool:
        return all(rule.outcome == TransitionRuleOutcome.OK for rule in self.rules)


class ErratumPushStatus(StrEnum):
    QUEUED = "QUEUED"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_ON_PUB = "WAITING_ON_PUB"
    POST_PUSH_PROCESSING = "POST_PUSH_PROCESSING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class ErratumPushDetails(BaseModel):
    status: ErratumPushStatus | None
    updated_at: datetime | None


class RHELVersion(BaseModel):
    major: int
    minor: int
    micro: int | None
    stream: str

    def __str__(self):
        if self.micro is not None:
            return f"RHEL-{self.major}.{self.minor}.{self.micro}.{self.stream}"
        return f"RHEL-{self.major}.{self.minor}.{self.stream}"

    @property
    def parent(self) -> "RHELVersion | None":
        """The release that the release inherits builds from."""
        if self.stream != "GA":
            return RHELVersion(
                major=self.major,
                minor=self.minor,
                micro=self.micro,
                stream="GA",
            )

        if self.minor > 0:
            one_minor_version_up = self.minor - 1
            match self.major:
                case 10:
                    return RHELVersion(
                        major=self.major,
                        minor=one_minor_version_up,
                        micro=self.micro,
                        stream="Z",
                    )
                case 9 | 8:
                    if one_minor_version_up % 2 == 1:
                        return RHELVersion(
                            major=self.major,
                            minor=one_minor_version_up,
                            micro=self.micro,
                            stream="Z.MAIN",
                        )
                    return RHELVersion(
                        major=self.major,
                        minor=one_minor_version_up,
                        micro=self.micro,
                        stream="Z.MAIN+EUS",
                    )

        return None

    @staticmethod
    def from_str(version_string: str) -> "RHELVersion | None":
        version_string = version_string.strip().upper()
        pattern = r"RHEL-(\d+)\.(\d+)(?:\.(\d+))?\.([^\d].*)$"
        match = re.match(pattern, version_string)
        if match is not None:
            version = RHELVersion(
                major=int(match.group(1)),
                minor=int(match.group(2)),
                micro=int(match.group(3)) if match.group(3) else None,
                stream=match.group(4),
            )
            if version_string != str(version):
                raise ValueError(f"round-trip mismatch: {version_string!r} != {str(version)!r}")
            return version
        return None


class RHELRelease(BaseModel):
    version: str
    ship_date: datetime | None  # None means already shipped

    @property
    def shipped(self):
        return self.ship_date is None or self.ship_date < datetime.now(tz=UTC)


class WorkflowResult(BaseModel):
    """Represents the result of running a workflow once."""

    status: str = Field(description="A message describing what happened during the workflow run and why")
    reschedule_in: float = Field(
        description="Delay in seconds to reschedule the work item. Negative value means don't reschedule"
    )
