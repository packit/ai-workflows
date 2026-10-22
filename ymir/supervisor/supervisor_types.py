"""Compatibility exports for supervisor model imports.

Shared provider and sweep models are defined in ``ymir.common.models``. The
legacy ``JotnarTag`` remains here because it has no common-package consumer.
"""

from typing import Literal

from pydantic import BaseModel

from ymir.common.models import (
    ErrataComment,
    ErrataStatus,
    Erratum,
    FullErratum,
    FullIssue,
    Issue,
    IssueStatus,
    JiraComment,
    PreliminaryTesting,
    TestCoverage,
    TestingFarmRequest,
    TestingFarmRequestResult,
    TestingFarmRequestState,
    TestingState,
    WorkflowResult,
)
from ymir.common.models import GitlabMergeRequest as MergeRequest
from ymir.common.models import GitlabMergeRequestState as MergeRequestState

Comment = ErrataComment

__all__ = [
    "Comment",
    "ErrataStatus",
    "Erratum",
    "FullErratum",
    "FullIssue",
    "Issue",
    "IssueStatus",
    "JiraComment",
    "JotnarTag",
    "MergeRequest",
    "MergeRequestState",
    "PreliminaryTesting",
    "TestCoverage",
    "TestingFarmRequest",
    "TestingFarmRequestResult",
    "TestingFarmRequestState",
    "TestingState",
    "WorkflowResult",
]


class JotnarTag(BaseModel):
    """A magic string associating an issue description with an erratum."""

    type: Literal["needs_attention"]
    resource: Literal["erratum"]
    id: str

    _LEGACY_PREFIXES = ("JOTNAR",)

    def __str__(self) -> str:
        return f"::: YMIR {self.type} E: {self.id.strip()} :::"

    def all_formats(self) -> list[str]:
        """Current and legacy tag strings for backwards-compatible search."""
        return [str(self)] + [
            f"::: {prefix} {self.type} E: {self.id.strip()} :::" for prefix in self._LEGACY_PREFIXES
        ]
