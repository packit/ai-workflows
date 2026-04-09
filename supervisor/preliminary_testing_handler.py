import logging
from typing import Any

from common.constants import JiraLabels

from .work_item_handler import WorkItemHandler
from .jira_utils import (
    add_issue_label,
    format_attention_message,
    get_issue_pull_requests,
    set_preliminary_testing,
)
from .supervisor_types import (
    FullIssue,
    IssueStatus,
    PreliminaryTesting,
    TestingState,
    WorkflowResult,
)
from .preliminary_testing_analyst import analyze_preliminary_testing

logger = logging.getLogger(__name__)


class PreliminaryTestingHandler(WorkItemHandler):
    """
    Perform preliminary testing evaluation for a JIRA issue.

    Checks GreenWave gating results and OSCI results posted as MR
    comments for the build fixing the issue, and sets the Preliminary
    Testing field to Pass if all tests have passed.
    """

    def __init__(
        self, issue: FullIssue, *, dry_run: bool, ignore_needs_attention: bool
    ):
        super().__init__(dry_run=dry_run, ignore_needs_attention=ignore_needs_attention)
        self.issue = issue

    def resolve_flag_attention(self, why: str, *, details_comment: str | None = None):
        if details_comment:
            full_comment = f"{format_attention_message(why)}\n\n{details_comment}"
        else:
            full_comment = format_attention_message(why)

        add_issue_label(
            self.issue.key,
            JiraLabels.NEEDS_ATTENTION.value,
            full_comment,
            dry_run=self.dry_run,
        )

        return WorkflowResult(status=why, reschedule_in=-1)

    def resolve_set_preliminary_testing_pass(self, comment: str) -> WorkflowResult:
        set_preliminary_testing(
            self.issue.key,
            PreliminaryTesting.PASS,
            comment,
            dry_run=self.dry_run,
        )

        return WorkflowResult(
            status="Preliminary testing passed", reschedule_in=-1
        )

    def find_pull_requests(self) -> list[dict[str, Any]]:
        """Find merge/pull requests linked to this issue via Jira dev-status API."""
        try:
            return get_issue_pull_requests(self.issue.key)
        except Exception as e:
            logger.warning(
                "Failed to get pull requests from Jira dev-status for %s: %s",
                self.issue.key,
                e,
            )
            return []

    async def run(self) -> WorkflowResult:
        issue = self.issue

        logger.info(
            "Running preliminary testing workflow for issue %s", issue.url
        )

        # Check for needs_attention label
        if (
            JiraLabels.NEEDS_ATTENTION.value in issue.labels
            and not self.ignore_needs_attention
        ):
            return self.resolve_remove_work_item(
                "Issue has the jotnar_needs_attention label"
            )

        # Validate single component
        if len(issue.components) != 1:
            return self.resolve_flag_attention(
                "This issue has multiple components. "
                "Jotnar only handles issues with single component currently."
            )

        # Check entry conditions
        if issue.status != IssueStatus.IN_PROGRESS:
            return self.resolve_remove_work_item(
                f"Issue status is {issue.status}, expected In Progress"
            )

        if issue.preliminary_testing == PreliminaryTesting.PASS:
            return self.resolve_remove_work_item(
                "Preliminary Testing is already set to Pass"
            )

        # Check if Test Coverage is filled
        test_coverage_missing = not issue.test_coverage

        build_nvr = issue.fixed_in_build

        # Find pull requests linked in Jira Development section
        pull_requests = self.find_pull_requests()
        if pull_requests:
            logger.info(
                "Found %d pull request(s) via Jira dev-status for %s",
                len(pull_requests),
                issue.key,
            )
        else:
            logger.warning(
                "No pull requests found for %s", issue.key,
            )

        # We need at least a build NVR or linked PRs to proceed
        if build_nvr is None and not pull_requests:
            return self.resolve_remove_work_item(
                "Issue has no Fixed in Build and no linked pull requests"
            )

        if build_nvr is None:
            logger.info(
                "Fixed in Build not set for %s, will analyze using MR results only",
                issue.key,
            )

        # Run the AI analysis with whatever data is available
        analysis = await analyze_preliminary_testing(
            jira_issue=issue,
            build_nvr=build_nvr,
            jira_pull_requests=pull_requests,
        )

        match analysis.state:
            case TestingState.PASSED:
                if test_coverage_missing:
                    return self.resolve_flag_attention(
                        "Preliminary tests passed but Test Coverage field is not set",
                        details_comment=analysis.comment,
                    )
                return self.resolve_set_preliminary_testing_pass(
                    analysis.comment
                    or "Preliminary testing has passed.",
                )
            case TestingState.FAILED:
                return self.resolve_flag_attention(
                    "Preliminary testing failed - see details below",
                    details_comment=analysis.comment,
                )
            case TestingState.PENDING:
                return self.resolve_wait("Preliminary tests are pending")
            case TestingState.RUNNING:
                return self.resolve_wait("Preliminary tests are running")
            case TestingState.NOT_RUNNING:
                return self.resolve_flag_attention(
                    "Preliminary tests are not running - see details below",
                    details_comment=analysis.comment,
                )
            case TestingState.ERROR:
                return self.resolve_flag_attention(
                    "An error occurred during preliminary testing analysis - see details below",
                    details_comment=analysis.comment,
                )
            case TestingState.WAIVED:
                if test_coverage_missing:
                    return self.resolve_flag_attention(
                        "Preliminary tests passed (waived) but Test Coverage field is not set",
                        details_comment=analysis.comment,
                    )
                return self.resolve_set_preliminary_testing_pass(
                    analysis.comment
                    or "Preliminary testing waived - non-blocking failures detected.",
                )
            case _:
                raise ValueError(
                    f"Unknown testing state: {analysis.state}"
                )
