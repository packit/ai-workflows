"""
Shared Pydantic models for the BeeAI agent system.

This module contains common data models used across different agents
and components to ensure consistency and type safety.
"""

from pydantic import BaseModel, Field
from typing import Dict, Any, Union
from pathlib import Path
from enum import Enum


class TriageInputSchema(BaseModel):
    """Input schema for the triage agent - metadata for a JIRA issue task."""
    issue: str = Field(description="JIRA issue key (e.g., RHEL-12345)")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for compatibility with existing code."""
        return self.model_dump()


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
        return cls(metadata=metadata.to_dict(), attempts=attempts)


# ============================================================================
# Rebase Agent Schemas
# ============================================================================

class RebaseInputSchema(BaseModel):
    """Input schema for the rebase agent."""
    local_clone: Path = Field(description="Path to the local clone of forked dist-git repository")
    package: str = Field(description="Package to update")
    dist_git_branch: str = Field(description="dist-git branch to update")
    version: str = Field(description="Version to update to")
    jira_issue: str = Field(description="Jira issue to reference as resolved")


class RebaseOutputSchema(BaseModel):
    """Output schema for the rebase agent."""
    success: bool = Field(description="Whether the rebase was successfully completed")
    status: str = Field(description="Rebase status")
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
    upstream_fix: str = Field(description="Link to an upstream fix for the issue")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    cve_id: str = Field(default="", description="CVE ID if the jira issue is a CVE")


class BackportOutputSchema(BaseModel):
    """Output schema for the backport agent."""
    success: bool = Field(description="Whether the backport was successfully completed")
    status: str = Field(description="Backport status with details of how the potential merge conflicts were resolved")
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
