import logging

from .errata_utils import get_erratum_for_link
from .work_item_handler import WorkItemHandler
from .jira_utils import add_issue_label, change_issue_status
from .supervisor_types import (
    FullIssue,
    IssueStatus,
    PreliminaryTesting,
    TestingState,
    WorkflowResult,
)
from .testing_analyst import analyze_issue


logger = logging.getLogger(__name__)


class IssueHandler(WorkItemHandler):
    """
    Perform a single step in the lifecycle of a JIRA issue.
    This includes changing the issue status, adding comments, and adding labels.
    """

    def __init__(self, issue: FullIssue, *, dry_run: bool):
        super().__init__(dry_run=dry_run)
        self.issue = issue

    def resolve_set_status(self, status: IssueStatus, why: str):
        change_issue_status(self.issue.key, status, why, dry_run=self.dry_run)

        if status in (IssueStatus.RELEASE_PENDING, IssueStatus.CLOSED):
            reschedule_delay = -1
        else:
            reschedule_delay = 0

        return WorkflowResult(status=why, reschedule_in=reschedule_delay)

    def resolve_flag_attention(self, why: str):
        add_issue_label(
            self.issue.key,
            "jotnar_needs_attention",
            why,
            dry_run=self.dry_run,
        )

        return WorkflowResult(status=why, reschedule_in=-1)

    async def run(self) -> WorkflowResult:
        """
        Runs the workflow for a single issue.
        """
        issue = self.issue

        logger.info("Running workflow for issue %s", issue.url)

        if issue.fixed_in_build is None:
            return self.resolve_remove_work_item("Issue has no fixed_in_build")

        if issue.preliminary_testing != PreliminaryTesting.PASS:
            return self.resolve_remove_work_item(
                "Issue has not passed preliminary_testing"
            )

        if issue.status in (
            IssueStatus.NEW,
            IssueStatus.PLANNING,
            IssueStatus.IN_PROGRESS,
        ):
            return self.resolve_set_status(
                IssueStatus.INTEGRATION,
                "Preliminary testing has passed, moving to Integration",
            )
        elif issue.status == IssueStatus.INTEGRATION:
            related_erratum = (
                get_erratum_for_link(issue.errata_link, full=True) if issue.errata_link else None
            )
            testing_analysis = await analyze_issue(issue, related_erratum)
            if testing_analysis.state == TestingState.NOT_RUNNING:
                return self.resolve_flag_attention(
                    testing_analysis.comment
                    or "Tests aren't running, and can't figure out how to run them. "
                    "(The testing analysis agent returned an empty comment)",
                )
            elif testing_analysis.state == TestingState.PENDING:
                return self.resolve_wait("Tests are pending")
            elif testing_analysis.state == TestingState.RUNNING:
                return self.resolve_wait("Tests are running")
            elif testing_analysis.state == TestingState.FAILED:
                return self.resolve_flag_attention(
                    testing_analysis.comment
                    or "Tests failed. "
                    "(The testing analysis agent returned an empty comment)",
                )
            elif testing_analysis.state == TestingState.PASSED:
                return self.resolve_set_status(
                    IssueStatus.RELEASE_PENDING,
                    testing_analysis.comment
                    or "Final testing has passed, moving to Release Pending. "
                    "(The testing analysis agent returned an empty comment)",
                )
            else:
                raise ValueError(f"Unknown testing state: {testing_analysis.state}")
        elif issue.status in (IssueStatus.RELEASE_PENDING, IssueStatus.CLOSED):
            return self.resolve_remove_work_item(f"Issue status is {issue.status}")
        else:
            raise ValueError(f"Unknown issue status: {issue.status}")
