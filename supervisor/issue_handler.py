from datetime import datetime, timezone, timedelta
import logging

from .errata_utils import get_erratum_for_link
from .work_item_handler import WorkItemHandler
from .jira_utils import add_issue_label, change_issue_status
from .gitlab_utils import (
    get_project_mr_merged_timestamp,
    search_gitlab_project_mrs,
)
from .supervisor_types import (
    FullIssue,
    IssueStatus,
    JotnarLabel,
    MergeRequestState,
    PreliminaryTesting,
    TestingState,
    WorkflowResult,
)
from .testing_analyst import analyze_issue


logger = logging.getLogger(__name__)
GROUPS = ["rhel", "centos-stream"]


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
            JotnarLabel.NEEDS_ATTENTION,
            why,
            dry_run=self.dry_run,
        )

        return WorkflowResult(status=why, reschedule_in=-1)

    def label_merged(self):
        """Add the jotnar_merged label to the issue

        Please check the issue passes the following prerequisites before
        calling this function:
            1. Issue has either jotnar_backported or jotnar_rebased label.
            2. Issue doesn't have jotnar_merged label.

        Returns:
            True if the jotnar_merged label is successfully added to the issue.
            Otherwise, returns False
        """
        issue = self.issue

        # Gating for single component issues
        if len(issue.components) != 1:
            return False

        for group in GROUPS:
            gen = search_gitlab_project_mrs(
                f"redhat/{group}/rpms/{issue.components[0]}",
                issue.key,
                state=MergeRequestState.MERGED,
            )

            while merged_mr := next(gen, None):
                add_issue_label(
                    issue.key,
                    JotnarLabel.MERGED,
                    f"A [merge request| {merged_mr.url}]. resolving this issue has been merged; waiting for errata creation and final testing.",
                    dry_run=self.dry_run,
                )
                return True

        return False

    def get_latest_merged_timestamp(self):
        issue = self.issue

        # Gating for single component issues
        if len(issue.components) != 1:
            return None

        latest_mr_timestamp = None
        component = issue.components[0]
        for group in GROUPS:
            project = f"redhat/{group}/rpms/{component}"
            for mr in search_gitlab_project_mrs(
                project,
                issue.key,
                state=MergeRequestState.MERGED,
            ):
                merged_timestamp = get_project_mr_merged_timestamp(project, mr.iid)

                latest_mr_timestamp = max(
                    latest_mr_timestamp,
                    merged_timestamp,
                    key=lambda t: t if t else datetime.min.replace(tzinfo=timezone.utc),
                )

        return latest_mr_timestamp

    async def run_before_errata_created(self) -> WorkflowResult:
        issue = self.issue
        labels = issue.labels

        # All issues in this branch should have no errata link

        if JotnarLabel.MERGED in labels:
            if latest_merged_timestamp := self.get_latest_merged_timestamp():
                cur_time = datetime.now(tz=timezone.utc)
                time_diff = abs(cur_time - latest_merged_timestamp)
                if time_diff < timedelta(days=1):
                    return self.resolve_wait(
                        "Wait for the associated erratum to be created",
                        reschedule_in=60 * 60,
                    )
                else:
                    return self.resolve_flag_attention(
                        "No errata link found after 24 hours after the MR got merged"
                    )

            return self.resolve_flag_attention(
                f"Issue has {JotnarLabel.MERGED} label, but no merged MRs found"
            )

        if JotnarLabel.BACKPORTED in labels or JotnarLabel.REBASED in labels:
            label_added = self.label_merged()

            if label_added:
                return WorkflowResult(
                    status="MR has been merged, put this back to queue for further process",
                    reschedule_in=0,
                )
            else:
                return self.resolve_wait(
                    "No merged MR found, reschedule it for 3 hours",
                    reschedule_in=60 * 60 * 3,
                )

        return self.resolve_remove_work_item(
            f"Issue without target labels: {issue.labels}"
        )

    async def run_after_errata_created(self) -> WorkflowResult:
        issue = self.issue
        if issue.fixed_in_build is None:
            return self.resolve_flag_attention(
                "Issue has errata_link but no fixed_in_build"
            )

        if issue.preliminary_testing != PreliminaryTesting.PASS:
            return self.resolve_flag_attention(
                "Issue does not have Preliminary Testing set to Pass - this should have "
                "happened before the gitlab pull request was merged"
            )

        if issue.test_coverage is None or len(issue.test_coverage) == 0:
            return self.resolve_flag_attention(
                "Issue does not have Test Coverage set - this should have "
                "happened before the gitlab pull request was merged"
            )

        labels = issue.labels
        if (
            JotnarLabel.BACKPORTED in labels or JotnarLabel.REBASED in labels
        ) and JotnarLabel.MERGED not in labels:
            self.label_merged()

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
            related_erratum = get_erratum_for_link(issue.errata_link, full=True)  # type: ignore
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

    async def run(self) -> WorkflowResult:
        """
        Runs the workflow for a single issue.
        """
        issue = self.issue

        logger.info("Running workflow for issue %s", issue.url)

        if JotnarLabel.NEEDS_ATTENTION in issue.labels:
            return self.resolve_remove_work_item(
                "Issue has the jotnar_needs_attention label"
            )

        if len(issue.components) != 1:
            return self.resolve_flag_attention(
                "This issue has multiple components."
                "Jotnar only handles issues with single component currently."
            )

        if issue.errata_link:
            return await self.run_after_errata_created()
        else:
            return await self.run_before_errata_created()
