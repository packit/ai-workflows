"""
Common Pydantic models shared across the BeeAI system.
"""

from typing import Optional
from pydantic import BaseModel, Field


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
    needs_internal_fix: Optional[bool] = Field(
        default=None,
        description="True for CVEs where internal fix is needed first (only applicable for CVEs)"
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if the issue cannot be processed"
    )
