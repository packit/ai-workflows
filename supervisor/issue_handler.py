from datetime import datetime, timezone, timedelta
import logging
from urllib.error import HTTPError

from supervisor.baseline_tests import BaselineTests

from common.constants import JiraLabels

from .constants import DATETIME_MIN_UTC, GITLAB_GROUPS
from .errata_utils import (
    get_erratum_for_link,
    get_previous_erratum,
)
from .gitlab_utils import search_gitlab_project_mrs
from .work_item_handler import WorkItemHandler
from .jira_utils import (
    add_issue_label,
    change_issue_status,
    format_attention_message,
    remove_issue_label,
    update_issue_comment,
)
from .supervisor_types import (
    Erratum,
    FullIssue,
    IssueStatus,
    MergeRequestState,
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

    def __init__(
        self, issue: FullIssue, *, dry_run: bool, ignore_needs_attention: bool
    ):
        super().__init__(dry_run=dry_run, ignore_needs_attention=ignore_needs_attention)
        self.issue = issue

    def resolve_set_status(self, status: IssueStatus, why: str):
        comment = f"*Changing status from {self.issue.status} => {status}*\n\n{why}"
        change_issue_status(self.issue.key, status, comment, dry_run=self.dry_run)

        if status in (IssueStatus.RELEASE_PENDING, IssueStatus.CLOSED):
            reschedule_delay = -1
        else:
            reschedule_delay = 0

        return WorkflowResult(status=why, reschedule_in=reschedule_delay)

    def resolve_flag_attention(self, why: str, *, details_comment: str | None = None):
        if details_comment:
            # panel first and testing analysis after that
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

    async def resolve_start_reproduction(
        self, related_erratum: Erratum, comment: str, failed_test_ids: list[str]
    ) -> WorkflowResult:
        assert self.issue.errata_link is not None

        def resolve_on_error(error_message: str) -> WorkflowResult:
            return self.resolve_flag_attention(
                "Tests failed - see details below. " + error_message,
                details_comment=comment,
            )

        previous_erratum_id, previous_build_nvr = get_previous_erratum(
            related_erratum.id, self.issue.components[0]
        )

        if previous_erratum_id is None:
            return resolve_on_error(
                "Cannot start reproduction with previous build - no previous erratum found to get build from."
            )

        if previous_build_nvr is None:
            return resolve_on_error(
                "Cannot start reproduction with previous build - error finding previous build NVR."
            )

        try:
            baseline_tests = BaselineTests.create(
                failure_comment=comment,
                failed_request_ids=failed_test_ids,
                previous_build_nvr=previous_build_nvr,
                dry_run=self.dry_run,
            )
        except Exception as e:
            if isinstance(e, HTTPError):
                logger.error("%s", e)
            else:
                logger.exception("%s", e)

            return resolve_on_error(str(e))

        issue_comment = baseline_tests.format_issue_comment()

        add_issue_label(
            self.issue.key,
            "jotnar_reproducing_tests",
            issue_comment,
            dry_run=self.dry_run,
        )

        return self.resolve_wait("Waiting to reproduce tests with previous build")

    async def resolve_check_reproduction(self) -> WorkflowResult:
        baseline_tests = BaselineTests.load_from_issue(self.issue)
        if baseline_tests is None:
            return self.resolve_flag_attention(
                "Issue has jotnar_reproducing_tests label but cannot parse baseline tests from comments"
            )

        if not baseline_tests.settled():
            return self.resolve_wait("Waiting for baseline tests to complete")

        await baseline_tests.create_attachments(
            issue_key=self.issue.key, dry_run=self.dry_run
        )

        issue_comment = baseline_tests.format_issue_comment(include_attachments=True)
        remove_issue_label(
            self.issue.key,
            "jotnar_reproducing_tests",
            dry_run=self.dry_run,
        )

        # Is always set by load_from_issue
        assert baseline_tests.comment_id is not None

        update_issue_comment(
            self.issue.key,
            baseline_tests.comment_id,
            issue_comment,
            dry_run=self.dry_run,
        )

        return WorkflowResult(
            status="Baseline tests are complete, will analyze results",
            reschedule_in=0.0,
        )

    def label_merge_if_needed(self):
        """Add the jotnar_merged label to the issue

        This function will only add jotnar_merged label to the issue if it matches
        all the following requirements:
            1. Issue has either jotnar_backported or jotnar_rebased label.
            2. Issue doesn't have jotnar_merged label.
            3. A merged MR is found on Gitlab.

        Returns:
            True if a merge gitlab issue was found and the merged label was added,
            otherwise, return False.
        """
        issue = self.issue
        component = issue.components[0]

        if (
            JiraLabels.BACKPORTED.value in issue.labels
            or JiraLabels.REBASED.value in issue.labels
        ) and JiraLabels.MERGED.value not in issue.labels:
            for group in GITLAB_GROUPS:
                merged_mrs = search_gitlab_project_mrs(
                    f"redhat/{group}/{component}",
                    issue.key,
                    state=MergeRequestState.MERGED,
                )

                if merged_mr := next(merged_mrs, None):
                    add_issue_label(
                        issue.key,
                        JiraLabels.MERGED.value,
                        f"A [merge request| {merged_mr.url}]. resolving this issue has been merged; waiting for errata creation and final testing.",
                        dry_run=self.dry_run,
                    )

                    issue.labels.append(JiraLabels.MERGED.value)
                    return True

        return False

    def get_latest_merged_timestamp(self):
        """This function will return DATETIME_MIN_UTC if it doesn't find any merged MRs"""
        issue = self.issue
        component = issue.components[0]

        def get_merged_mrs():
            for group in GITLAB_GROUPS:
                project = f"redhat/{group}/{component}"
                yield from search_gitlab_project_mrs(
                    project,
                    issue.key,
                    state=MergeRequestState.MERGED,
                )

        return max(
            (mr.merged_at or DATETIME_MIN_UTC for mr in get_merged_mrs()),
            default=DATETIME_MIN_UTC,
        )

    async def run_before_errata_created(self) -> WorkflowResult:
        """Workflow for issues with no errata link"""
        issue = self.issue

        if not any(
            label
            in (
                JiraLabels.BACKPORTED.value,
                JiraLabels.REBASED.value,
                JiraLabels.MERGED.value,
            )
            for label in issue.labels
        ):
            return self.resolve_remove_work_item(
                f"Issue without target labels: {issue.labels}"
            )

        if JiraLabels.MERGED.value not in issue.labels:
            self.label_merge_if_needed()

        if JiraLabels.MERGED.value not in issue.labels:
            return self.resolve_wait(
                "No merged MR found, reschedule it for 3 hours",
                reschedule_in=60 * 60 * 3,
            )

        latest_merged_timestamp = self.get_latest_merged_timestamp()
        cur_time = datetime.now(tz=timezone.utc)
        time_diff = abs(cur_time - latest_merged_timestamp)
        if time_diff < timedelta(days=1):
            return self.resolve_wait(
                "Wait for the associated erratum to be created",
                reschedule_in=60 * 60,
            )
        else:
            return self.resolve_flag_attention(
                "A merge request was merged for this issue more than 24 hours ago but no errata "
                "was created. Please investigate and look for gating failures or other reasons that "
                "might have blocked errata creation."
            )

    async def run_after_errata_created(self) -> WorkflowResult:
        issue = self.issue
        assert issue.errata_link is not None
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

        # We still want the jotnar_merged label for JIRA dashboards even if we never saw
        # the merged merge request in the pre-errata-creation state.
        self.label_merge_if_needed()

        match issue.status:
            case IssueStatus.NEW | IssueStatus.PLANNING | IssueStatus.IN_PROGRESS:
                return self.resolve_set_status(
                    IssueStatus.INTEGRATION,
                    "Preliminary testing has passed, moving to Integration",
                )
            case IssueStatus.INTEGRATION:
                if "jotnar_reproducing_tests" in issue.labels:
                    return await self.resolve_check_reproduction()

                related_erratum = get_erratum_for_link(issue.errata_link, full=True)

                baseline_tests = BaselineTests.load_from_issue(self.issue)
                if baseline_tests is not None:
                    testing_analysis = await analyze_issue(
                        issue, related_erratum, after_baseline=True
                    )
                else:
                    testing_analysis = await analyze_issue(issue, related_erratum)

                match testing_analysis.state:
                    case TestingState.NOT_RUNNING:
                        return self.resolve_flag_attention(
                            "Tests aren't running - see details below",
                            details_comment=testing_analysis.comment,
                        )
                    case TestingState.PENDING:
                        return self.resolve_wait("Tests are pending")
                    case TestingState.RUNNING:
                        return self.resolve_wait("Tests are running")
                    case TestingState.FAILED:
                        if testing_analysis.failed_test_ids and (
                            baseline_tests is None
                            or (
                                set(testing_analysis.failed_test_ids)
                                != set(c.failed.id for c in baseline_tests.comparisons)
                            )
                        ):
                            return await self.resolve_start_reproduction(
                                related_erratum,
                                testing_analysis.comment or "",
                                testing_analysis.failed_test_ids,
                            )
                        else:
                            return self.resolve_flag_attention(
                                "Tests failed - see details below",
                                details_comment=testing_analysis.comment,
                            )
                    case TestingState.ERROR:
                        return self.resolve_flag_attention(
                            "An error occurred during testing - see details below",
                            details_comment=testing_analysis.comment,
                        )
                    case TestingState.PASSED:
                        return self.resolve_set_status(
                            IssueStatus.RELEASE_PENDING,
                            testing_analysis.comment or "Final testing has passed.",
                        )
                    case TestingState.WAIVED:
                        return self.resolve_set_status(
                            IssueStatus.RELEASE_PENDING,
                            testing_analysis.comment
                            or "Final testing has been waived, moving to Release Pending.",
                        )
                    case _:
                        raise ValueError(
                            f"Unknown testing state: {testing_analysis.state}"
                        )
            case IssueStatus.RELEASE_PENDING | IssueStatus.CLOSED:
                return self.resolve_remove_work_item(f"Issue status is {issue.status}")
            case _:
                raise ValueError(f"Unknown issue status: {issue.status}")

    async def run(self) -> WorkflowResult:
        """
        Runs the workflow for a single issue.
        """
        issue = self.issue

        logger.info("Running workflow for issue %s", issue.url)

        if (
            JiraLabels.NEEDS_ATTENTION.value in issue.labels
            and not self.ignore_needs_attention
        ):
            return self.resolve_remove_work_item(
                "Issue has the jotnar_needs_attention label"
            )

        if len(issue.components) != 1:
            return self.resolve_flag_attention(
                "This issue has multiple components. "
                "Jotnar only handles issues with single component currently."
            )

        if issue.errata_link is None:
            return await self.run_before_errata_created()
        else:
            return await self.run_after_errata_created()
