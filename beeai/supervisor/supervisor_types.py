from datetime import datetime
from enum import StrEnum
from typing import Optional
from pydantic import BaseModel, Field


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
    PASS = "Pass"
    READY = "Ready"


class ErrataStatus(StrEnum):
    NEW_FILES = "NEW_FILES"
    QE = "QE"
    REL_PREP = "REL_PREP"
    PUSH_READY = "PUSH_READY"
    IN_PUSH = "IN_PUSH"
    DROPPED_NO_SHIP = "DROPPED_NO_SHIP"
    SHIPPED_LIVE = "SHIPPED_LIVE"


class Erratum(BaseModel):
    id: int
    full_advisory: str
    url: str
    synopsis: str
    status: ErrataStatus
    all_issues_release_pending: bool


class MergeRequestState(StrEnum):
    OPEN = "opened"
    CLOSED = "closed"
    MERGED = "merged"


class MergeRequest(BaseModel):
    project: str
    iid: int
    url: str
    title: str
    description: str
    state: MergeRequestState


class Issue(BaseModel):
    key: str
    url: str
    summary: str
    components: list[str]
    status: IssueStatus
    fix_versions: list[str]
    errata_link: Optional[str]  # RHEL only
    fixed_in_build: str | None = None  # RHEL only
    test_coverage: list[TestCoverage] | None = None  # RHEL only
    preliminary_testing: PreliminaryTesting | None = None  # RHEL only


class IssueComment(BaseModel):
    authorName: str
    authorEmail: str
    created: datetime
    body: str


class FullIssue(Issue):
    comments: list[IssueComment]


class TestingState(StrEnum):
    NOT_RUNNING = "tests-not-running"
    PENDING = "tests-pending"
    RUNNING = "tests-running"
    FAILED = "tests-failed"
    PASSED = "tests-passed"


class WorkflowResult(BaseModel):
    status: str
    reschedule_in: float = Field(
        description="Delay in seconds to reschedule the task. Negative value means don't reschedule"
    )
