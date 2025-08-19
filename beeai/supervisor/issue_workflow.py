import logging

from .supervisor_types import (
    Issue,
    IssueStatus,
    PreliminaryTesting,
    TestingState,
    WorkflowResult,
)
from .testing_analyst import analyze_issue


logger = logging.getLogger(__name__)


WAIT_DELAY = 20 * 60  # 20 minutes


def resolve_remove_task(issue: Issue, why: str):
    return WorkflowResult(status=why, reschedule_in=-1)


def resolve_wait(issue: Issue, why: str):
    return WorkflowResult(status=why, reschedule_in=WAIT_DELAY)


def resolve_set_status(issue: Issue, status: IssueStatus, why: str):
    if status in (IssueStatus.RELEASE_PENDING, IssueStatus.CLOSED):
        reschedule_delay = -1
    else:
        reschedule_delay = 0

    return WorkflowResult(status=why, reschedule_in=reschedule_delay)


def resolve_flag_attention(issue: Issue, why: str):
    return WorkflowResult(status=why, reschedule_in=-1)


async def run_issue_workflow(issue: Issue) -> WorkflowResult:
    """
    Runs the workflow for a single issue.
    """
    logger.info("Running workflow for issue %s", issue.url)

    if issue.fixed_in_build is None:
        return resolve_remove_task(issue, "Issue has no fixed_in_build")

    if issue.preliminary_testing != PreliminaryTesting.PASS:
        return resolve_remove_task(issue, "Issue has not passed preliminary_testing")

    if issue.status in (IssueStatus.NEW, IssueStatus.IN_PROGRESS):
        return resolve_set_status(
            issue,
            IssueStatus.INTEGRATION,
            "Preliminary testing has passed, moving to Integration",
        )
    elif issue.status == IssueStatus.INTEGRATION:
        testing_analysis = await analyze_issue(issue.key)
        if testing_analysis.state == TestingState.NOT_RUNNING:
            return resolve_flag_attention(
                issue,
                testing_analysis.comment
                or "Tests aren't running, and can't figure out how to run them. "
                "(The testing analysis agent returned an empty comment)",
            )
        elif testing_analysis.state == TestingState.PENDING:
            return resolve_wait(issue, "Tests are pending")
        elif testing_analysis.state == TestingState.RUNNING:
            return resolve_wait(issue, "Tests are running")
        elif testing_analysis.state == TestingState.FAILED:
            return resolve_flag_attention(
                issue,
                testing_analysis.comment
                or "Tests failed. "
                "(The testing analysis agent returned an empty comment)",
            )
        elif testing_analysis.state == TestingState.PASSED:
            return resolve_set_status(
                issue,
                IssueStatus.RELEASE_PENDING,
                testing_analysis.comment
                or "Final testing has passed, moving to Release Pending. "
                "(The testing analysis agent returned an empty comment)",
            )
        else:
            raise ValueError(f"Unknown testing state: {testing_analysis.state}")
    elif issue.status in (IssueStatus.RELEASE_PENDING, IssueStatus.CLOSED):
        return resolve_remove_task(issue, f"Issue status is {issue.status}")
    else:
        raise ValueError(f"Unknown issue status: {issue.status}")