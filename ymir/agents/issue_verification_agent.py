import asyncio
import logging
import os
import sys
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any

from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.template import PromptTemplate, PromptTemplateInput
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field

from ymir.agents.observability import setup_observability
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utilities.baseline_tests import BaselineTests
from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    mcp_tools,
    run_tool,
)
from ymir.common.constants import DATETIME_MIN_UTC, GITLAB_GROUPS, JiraLabels
from ymir.common.logging_setup import configure_logging
from ymir.common.models import (
    FullErratum,
    FullIssue,
    GitlabMergeRequest,
    GitlabMergeRequestState,
    IssueStatus,
    JiraComment,
    PreliminaryTesting,
    TestCoverage,
    TestingState,
    WorkflowResult,
)
from ymir.tools.privileged.jira import GetJiraAttachmentTool
from ymir.tools.unprivileged.analyze_ewa_testrun import AnalyzeEwaTestRunTool
from ymir.tools.unprivileged.read_logfile import ReadLogfileTool
from ymir.tools.unprivileged.read_readme import ReadReadmeTool
from ymir.tools.unprivileged.search_resultsdb import SearchResultsdbTool

logger = logging.getLogger(__name__)

WAIT_DELAY = 20 * 60  # 20 minutes
MERGE_CHECK_DELAY = 3 * 60 * 60  # 3 hours
ERRATA_WAIT_DELAY = 60 * 60  # 1 hour

ATTENTION_TEMPLATE = (
    "{{panel:title=Project Ymir: ATTENTION NEEDED|"
    "borderStyle=solid|borderColor=#CC0000|titleBGColor=#FFF5F5|bgColor=#FFFEF0}}\n"
    "{why}\n\n"
    "Please resolve this and remove the {{ymir_needs_attention}} flag.\n"
    "{{panel}}"
)

# --- Testing analyst schemas and prompts ---


class TestingAnalystInput(BaseModel):
    issue: FullIssue = Field(description="Details of JIRA issue to analyze")
    maintainer_rules: str = Field(
        description="Maintainer-defined rules and guidelines for the package (from AGENTS.md)"
    )
    erratum: FullErratum = Field(description="Details of the related ERRATUM")
    current_time: datetime = Field(description="Current timestamp")


class TestingAnalystOutput(BaseModel):
    state: TestingState = Field(description="State of tests")
    comment: str | None = Field(description="Comment to add to the JIRA issue")
    failed_test_ids: list[str] | None = Field(description="List of Testing Farm run IDs with failures")


TESTING_ANALYST_TEMPLATE_COMMON = """\
You are the testing analyst agent for Project Ymir. Comments that tag
[~jotnar-project] in JIRA issues are directed to you and other Ymir agents
sharing the same account—pay close attention to these.

Your task is to analyze a RHEL JIRA issue with a fix attached and determine
the state of testing and what needs to be done.

JIRA_ISSUE_DATA: {{ issue }}
ERRATUM_DATA: {{ erratum }}
MAINTAINER_RULES: {{ maintainer_rules }}
CURRENT_TIME: {{ current_time }}
"""

TESTING_ANALYST_TEMPLATE_NORMAL = (
    TESTING_ANALYST_TEMPLATE_COMMON
    + """
For components handled by the New Errata Workflow Automation(NEWA):
NEWA will post a comment to the erratum when it has started tests and when they finish.
Read the JIRA issue in those comments to find test results.
For components handled by Errata Workflow Automation (EWA):
EWA will post a comment to the erratum when it has started tests and when they finish.
Read the comment to find the test results in TCMS Test Run.

If the maintainer rules say that tests are started by NEWA, but there are no comments
from NEWA providing links to JIRA issues, then this component may be a component where
NEWA is only used for RHEL10, and not earlier versions - in that case, you may read the
results from the TCMS test run posted by EWA.

In all other cases, if the tests are supposed to be started by NEWA, ignore any comments with
links to TCMS or Beaker.

IMPORTANT: OSCI gating tests run as part of the GitLab merge request pipeline and
they do NOT constitute final testing. You must find evidence of full integration
and regression testing triggered by NEWA or EWA (posted as comments on the erratum)
before concluding tests-passed or tests-waived. If only OSCI gating results are
available, return tests-pending.

You cannot assume that tests have passed just because a comment says they have
finished, it is mandatory to check the actual test results in the JIRA issue or TCMS.
Make sure that the JIRA issue or TCMS Test Run is the correct one for the latest build in the
erratum.

Tests can trigger at various points in an issue's lifecycle depending on component
configuration, but always by the time the erratum moves to QE status. If the erratum
is in QE status, and its last_status_transition_timestamp is more than 6 hours ago,
and there's no evidence from erratum comments of tests running or completed, then assume
tests will not run automatically and return tests-not-running.

Call the final_answer tool passing in the state and a comment as follows.
The comment should use JIRA comment syntax.

If the tests need to be started manually:
    state: tests-not-running
    comment: [explain what needs to be done to start tests]

If the tests are complete and failed:
    state: tests-failed
    comment: [list failed tests with URLs]
    failed_test_ids: [list of IDs for testing farm runs that failed]

If the tests are complete and passed:
    state: tests-passed
    comment: [Give a brief summary of what was tested with a link to the result.]

If there are *some* test failures, but you are sure they are not regressions
and most tests complete successfully:
    state: tests-waived
    comment: [Explain which tests failed and why they are not considered regressions]

If the tests will be started automatically without user intervention, but are not yet running:
    state: tests-pending
    comment: [Provide a brief description of what tests are expected to run and where the results will be]

If the tests are currently running:
    state: tests-running
    comment: [Provide a brief description of what tests are running and where the results will be]

If tests have not started or completed when they should have (as described above):
    state: tests-not-running
    comment: [Explain the situation and that manual intervention is needed]
"""
)

TESTING_ANALYST_TEMPLATE_AFTER_BASELINE = (
    TESTING_ANALYST_TEMPLATE_COMMON
    + """
You have previously analyzed this issue and identified failing test runs. These
tests have now been repeated with a baseline build to determine if the failures
are due to issues in the new build, or whether the tests were already failing.

Please read the comments in the JIRA issue related to find the results of the
baseline test runs, and update your analysis accordingly. The detailed results
of the comparison will be found in the attachments to the JIRA issue. Make
sure to read these attachments for all architectures.

You can read logfiles to help with your analysis using the read_logfile tool.

If all tests that failed with the new build also failed with the baseline build,
then it is likely that there are no regressions in the new build. However,
you should examine a selection of log files to make sure that the failures are
consistent between the two runs, and that the failures are not due to some basic
failure of the test environment (e.g. misconfiguration, missing dependencies,
infrastructure issues) that would obscure real regressions.

An appropriate number of log files to examine in this case is typically 2-3 per
architecture.

Call the final_answer tool passing in the state and a comment as follows.
The comment should use JIRA comment syntax. If it seems useful, please include
a table in the output comment summarizing the results per architecture.

If an error prevented tests from running on the new build:
    state: tests-error
    comment: explanation of why tests could not be run, if available

If the tests failures seem to reflect a regression in the new build compared to the baseline:
    state: tests-failed
    comment: detailed description of the tests that are failing, and if known, possible reasons why

If you are uncertain whether the failures are due to a regression or not:
    state: tests-failed
    comment: detailed description about what might reflect a regression

If it seems highly likely that there are no regressions:
    state: tests-waived
    comment: description of why the failures are not likely to be regressions

 Do not use tests-waived option if tests could not be run on the new build.
"""
)


def _render_testing_analyst_prompt(input: TestingAnalystInput, after_baseline: bool) -> str:
    template = TESTING_ANALYST_TEMPLATE_AFTER_BASELINE if after_baseline else TESTING_ANALYST_TEMPLATE_NORMAL
    return PromptTemplate(PromptTemplateInput(schema=TestingAnalystInput, template=template)).render(input)


async def _analyze_testing_results(
    jira_issue: FullIssue,
    erratum: FullErratum,
    gateway_tools: list,
    after_baseline: bool = False,
) -> TestingAnalystOutput:
    """Run the testing analyst sub-agent to analyze test results."""
    tools = [
        ThinkTool(),
        GetJiraAttachmentTool(),
        ReadLogfileTool(),
        ReadReadmeTool(),
        SearchResultsdbTool(),
        AnalyzeEwaTestRunTool(),
    ]

    agent = ReasoningAgent(
        name="TestingAnalyst",
        description="Agent that analyzes JIRA issues and determines the state of testing for RHEL errata",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=tools,
        memory=UnconstrainedMemory(),
    )

    maintainer_rules = await run_tool(
        "get_maintainer_rules",
        available_tools=gateway_tools,
        package=jira_issue.components[0],
    )

    input = TestingAnalystInput(
        issue=jira_issue,
        maintainer_rules=maintainer_rules,
        erratum=erratum,
        current_time=datetime.now(UTC),
    )

    response = await agent.run(
        _render_testing_analyst_prompt(input, after_baseline=after_baseline),
        expected_output=TestingAnalystOutput,
        **get_agent_execution_config(),  # type: ignore
    )
    if response.state.result is None:
        raise ValueError("Agent did not return a result")
    output = TestingAnalystOutput.model_validate_json(response.state.result.text)
    logger.info("Testing analysis completed: %s", output.model_dump_json(indent=4))
    return output


class IssueVerificationWorkflowState(BaseModel):
    jira_issue: str
    dry_run: bool = False
    ignore_needs_attention: bool = False

    issue_data: dict[str, Any] | None = Field(default=None)
    issue: FullIssue | None = Field(default=None)
    result: WorkflowResult | None = Field(default=None)


def _parse_issue_from_jira_data(issue_data: dict[str, Any]) -> FullIssue:
    """Parse a FullIssue from raw JIRA API response data."""
    fields = issue_data.get("fields", {})
    key = issue_data["key"]
    jira_url = os.environ.get("JIRA_URL", "https://redhat.atlassian.net").rstrip("/")

    def _get_custom_field(name: str) -> Any:
        """Look up a custom field by known name mappings."""
        known_fields = {
            "Errata Link": "customfield_10418",
            "Fixed in Build": "customfield_10578",
            "Test Coverage": "customfield_10638",
            "Preliminary Testing": "customfield_10879",
            "AssignedTeam": "customfield_10371",
        }
        field_id = known_fields.get(name)
        if field_id:
            return fields.get(field_id)
        return None

    def _get_enum_value(data: Any) -> str | None:
        if isinstance(data, dict):
            return data.get("value")
        return None

    def _get_enum_list(data: Any) -> list[str] | None:
        if data is None:
            return None
        if isinstance(data, list):
            return [d.get("value") for d in data if isinstance(d, dict) and d.get("value")]
        return None

    errata_link = _get_custom_field("Errata Link") or fields.get("customfield_10626")
    assigned_team = _get_custom_field("AssignedTeam")
    assigned_team_name = assigned_team.get("value") if isinstance(assigned_team, dict) else None

    test_coverage_raw = _get_custom_field("Test Coverage")
    test_coverage = None
    if test_coverage_raw:
        tc_values = _get_enum_list(test_coverage_raw)
        if tc_values:
            test_coverage = [TestCoverage(v) for v in tc_values]

    preliminary_testing_raw = _get_custom_field("Preliminary Testing")
    preliminary_testing = None
    if preliminary_testing_raw:
        pt_value = _get_enum_value(preliminary_testing_raw)
        if pt_value:
            preliminary_testing = PreliminaryTesting(pt_value)

    # Extract description - handle both plain text (v2) and ADF (v3) formats
    description_raw = fields.get("description", "")
    if isinstance(description_raw, dict):
        # ADF format - extract text content
        def _extract_adf_text(node: Any) -> str:
            if isinstance(node, str):
                return node
            if isinstance(node, dict):
                if node.get("type") == "text":
                    return node.get("text", "")
                content = node.get("content", [])
                return "".join(_extract_adf_text(c) for c in content)
            if isinstance(node, list):
                return "".join(_extract_adf_text(c) for c in node)
            return ""

        description = _extract_adf_text(description_raw)
    else:
        description = description_raw or ""

    # Parse comments
    comments_data = fields.get("comment", {})
    if isinstance(comments_data, dict):
        comments_list = comments_data.get("comments", [])
    elif isinstance(comments_data, list):
        comments_list = comments_data
    else:
        comments_list = []

    comments = [
        JiraComment(
            authorName=c["author"].get("displayName", "Unknown"),
            authorEmail=c["author"].get("emailAddress"),
            created=datetime.fromisoformat(c["created"]),
            body=c.get("body", ""),
            id=c["id"],
        )
        for c in comments_list
    ]

    return FullIssue(
        key=key,
        url=f"{jira_url}/browse/{key}",
        assigned_team=assigned_team_name,
        summary=fields.get("summary", ""),
        status=IssueStatus(fields.get("status", {}).get("name", "New")),
        components=[c["name"] for c in fields.get("components", [])],
        labels=fields.get("labels", []),
        fix_versions=[v["name"] for v in fields.get("fixVersions", [])],
        errata_link=errata_link,
        fixed_in_build=_get_custom_field("Fixed in Build"),
        test_coverage=test_coverage,
        preliminary_testing=preliminary_testing,
        description=description,
        comments=comments,
    )


async def _add_label(issue_key: str, label: str, comment: str | None, tools: list, dry_run: bool) -> None:
    if dry_run:
        logger.info("Dry run: would add label %s to issue %s", label, issue_key)
        return

    await run_tool(
        "edit_jira_labels",
        available_tools=tools,
        issue_key=issue_key,
        labels_to_add=[label],
    )
    if comment:
        await run_tool(
            "add_jira_comment",
            available_tools=tools,
            issue_key=issue_key,
            comment=comment,
            private=True,
        )


async def _remove_label(issue_key: str, label: str, tools: list, dry_run: bool) -> None:
    if dry_run:
        logger.info("Dry run: would remove label %s from issue %s", label, issue_key)
        return

    await run_tool(
        "edit_jira_labels",
        available_tools=tools,
        issue_key=issue_key,
        labels_to_remove=[label],
    )


async def _flag_attention(
    issue_key: str,
    why: str,
    *,
    details_comment: str | None = None,
    tools: list,
    dry_run: bool,
) -> WorkflowResult:
    full_comment = ATTENTION_TEMPLATE.format(why=why)
    if details_comment:
        full_comment = f"{full_comment}\n\n{details_comment}"

    await _add_label(issue_key, JiraLabels.NEEDS_ATTENTION.value, full_comment, tools, dry_run)
    return WorkflowResult(status=why, reschedule_in=-1)


async def _change_status(
    issue_key: str,
    current_status: IssueStatus,
    new_status: IssueStatus,
    why: str,
    tools: list,
    dry_run: bool,
) -> WorkflowResult:
    comment = f"*Changing status from {current_status} => {new_status}*\n\n{why}"

    if dry_run:
        logger.info("Dry run: would change issue %s status to %s", issue_key, new_status)
    else:
        await run_tool(
            "change_jira_status",
            available_tools=tools,
            issue_key=issue_key,
            status=str(new_status),
        )
        await run_tool(
            "add_jira_comment",
            available_tools=tools,
            issue_key=issue_key,
            comment=comment,
            private=True,
        )

    reschedule_delay = -1 if new_status in (IssueStatus.RELEASE_PENDING, IssueStatus.CLOSED) else 0
    return WorkflowResult(status=why, reschedule_in=reschedule_delay)


async def _search_merged_mrs(
    component: str, issue_key: str, state: str, tools: list
) -> list[GitlabMergeRequest]:
    """Search for merge requests across all GitLab groups."""
    results = []
    for group in GITLAB_GROUPS:
        project = f"redhat/{group}/{component}"
        try:
            mrs_data = await run_tool(
                "search_gitlab_project_mrs",
                available_tools=tools,
                project=project,
                search=issue_key,
                state=state,
            )
            results.extend(GitlabMergeRequest(**mr_data) for mr_data in mrs_data if isinstance(mr_data, dict))
        except Exception as e:
            logger.warning("Error searching MRs in %s: %s", project, e)
    return results


async def _label_merge_if_needed(issue: FullIssue, tools: list, dry_run: bool) -> bool:
    """Add ymir_merged label if a merged MR exists and the issue has backported/rebased label."""
    component = issue.components[0]

    if (
        JiraLabels.BACKPORTED.value in issue.labels or JiraLabels.REBASED.value in issue.labels
    ) and JiraLabels.MERGED.value not in issue.labels:
        merged_mrs = await _search_merged_mrs(component, issue.key, GitlabMergeRequestState.MERGED, tools)
        if merged_mrs:
            merged_mr = merged_mrs[0]
            await _add_label(
                issue.key,
                JiraLabels.MERGED.value,
                f"A [merge request|{merged_mr.url}]. resolving this issue "
                "has been merged; waiting for errata creation and final testing.",
                tools,
                dry_run,
            )
            issue.labels.append(JiraLabels.MERGED.value)
            return True

    return False


async def _get_latest_merged_timestamp(issue: FullIssue, tools: list) -> datetime:
    """Get the latest merged timestamp from all merged MRs."""
    component = issue.components[0]
    merged_mrs = await _search_merged_mrs(component, issue.key, GitlabMergeRequestState.MERGED, tools)
    if not merged_mrs:
        return DATETIME_MIN_UTC
    return max(
        (mr.merged_at or DATETIME_MIN_UTC for mr in merged_mrs),
        default=DATETIME_MIN_UTC,
    )


async def run_issue_verification(
    jira_issue: str,
    dry_run: bool = False,
    ignore_needs_attention: bool = False,
) -> WorkflowResult:
    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        workflow = Workflow(IssueVerificationWorkflowState, name="IssueVerificationWorkflow")

        async def fetch_and_validate_issue(state: IssueVerificationWorkflowState):
            """Fetch JIRA issue data and validate preconditions."""
            logger.info("Fetching JIRA issue data for %s", state.jira_issue)
            state.issue_data = await run_tool(
                "get_jira_details",
                available_tools=gateway_tools,
                issue_key=state.jira_issue,
            )

            state.issue = _parse_issue_from_jira_data(state.issue_data)
            issue = state.issue

            logger.info("Running workflow for issue %s", issue.url)

            if JiraLabels.NEEDS_ATTENTION.value in issue.labels and not state.ignore_needs_attention:
                state.result = WorkflowResult(
                    status="Issue has the ymir_needs_attention label",
                    reschedule_in=-1,
                )
                return Workflow.END

            if len(issue.components) != 1:
                state.result = await _flag_attention(
                    issue.key,
                    "This issue has multiple components. "
                    "Ymir only handles issues with single component currently.",
                    tools=gateway_tools,
                    dry_run=state.dry_run,
                )
                return Workflow.END

            return "check_errata_status"

        async def check_errata_status(state: IssueVerificationWorkflowState):
            """Branch based on whether errata link exists."""
            if state.issue.errata_link is None:
                return "run_before_errata"
            return "run_after_errata"

        async def run_before_errata(state: IssueVerificationWorkflowState):
            """Handle issues without errata link."""
            issue = state.issue

            if not any(
                label
                in (
                    JiraLabels.BACKPORTED.value,
                    JiraLabels.REBASED.value,
                    JiraLabels.MERGED.value,
                )
                for label in issue.labels
            ):
                state.result = WorkflowResult(
                    status=f"Issue without target labels: {issue.labels}",
                    reschedule_in=-1,
                )
                return Workflow.END

            if JiraLabels.MERGED.value not in issue.labels:
                await _label_merge_if_needed(issue, gateway_tools, state.dry_run)

            if JiraLabels.MERGED.value not in issue.labels:
                state.result = WorkflowResult(
                    status=f"No merged MR found, reschedule in {MERGE_CHECK_DELAY}s",
                    reschedule_in=MERGE_CHECK_DELAY,
                )
                return Workflow.END

            latest_merged_timestamp = await _get_latest_merged_timestamp(issue, gateway_tools)
            cur_time = datetime.now(tz=UTC)
            time_diff = abs(cur_time - latest_merged_timestamp)
            if time_diff < timedelta(days=1):
                state.result = WorkflowResult(
                    status=f"Wait for the associated erratum to be created, "
                    f"reschedule in {ERRATA_WAIT_DELAY}s",
                    reschedule_in=ERRATA_WAIT_DELAY,
                )
                return Workflow.END

            state.result = await _flag_attention(
                issue.key,
                "A merge request was merged for this issue more than 24 hours ago but no errata "
                "was created. Please investigate and look for gating failures or other reasons "
                "that might have blocked errata creation.",
                tools=gateway_tools,
                dry_run=state.dry_run,
            )
            return Workflow.END

        async def run_after_errata(state: IssueVerificationWorkflowState):
            """Handle issues with errata link."""
            issue = state.issue
            if issue.errata_link is None:
                raise ValueError("errata_link must be set before run_after_errata")

            if issue.fixed_in_build is None:
                state.result = await _flag_attention(
                    issue.key,
                    "Issue has errata_link but no fixed_in_build",
                    tools=gateway_tools,
                    dry_run=state.dry_run,
                )
                return Workflow.END

            if issue.preliminary_testing != PreliminaryTesting.PASS:
                state.result = await _flag_attention(
                    issue.key,
                    "Issue does not have Preliminary Testing set to Pass - this should have "
                    "happened before the gitlab pull request was merged",
                    tools=gateway_tools,
                    dry_run=state.dry_run,
                )
                return Workflow.END

            if issue.test_coverage is None or len(issue.test_coverage) == 0:
                state.result = await _flag_attention(
                    issue.key,
                    "Issue does not have Test Coverage set - this should have "
                    "happened before the gitlab pull request was merged",
                    tools=gateway_tools,
                    dry_run=state.dry_run,
                )
                return Workflow.END

            # Add merged label even in post-errata state for JIRA dashboards
            await _label_merge_if_needed(issue, gateway_tools, state.dry_run)

            match issue.status:
                case IssueStatus.NEW | IssueStatus.PLANNING | IssueStatus.IN_PROGRESS:
                    state.result = await _change_status(
                        issue.key,
                        issue.status,
                        IssueStatus.INTEGRATION,
                        "Preliminary testing has passed, moving to Integration",
                        gateway_tools,
                        state.dry_run,
                    )
                    return Workflow.END
                case IssueStatus.INTEGRATION:
                    if "ymir_reproducing_tests" in issue.labels:
                        return "check_reproduction"
                    return "analyze_testing"
                case IssueStatus.RELEASE_PENDING | IssueStatus.CLOSED:
                    state.result = WorkflowResult(
                        status=f"Issue status is {issue.status}",
                        reschedule_in=-1,
                    )
                    return Workflow.END
                case _:
                    raise ValueError(f"Unknown issue status: {issue.status}")

        async def analyze_testing(state: IssueVerificationWorkflowState):
            """Call testing_analyst sub-agent for test result analysis."""
            issue = state.issue
            if issue.errata_link is None:
                raise ValueError("errata_link must be set before analyze_testing")

            # Get erratum data
            erratum_data = await run_tool(
                "get_erratum",
                available_tools=gateway_tools,
                erratum_id=issue.errata_link,
                full=True,
            )
            related_erratum = FullErratum(**erratum_data)

            # Check if baseline tests were already run
            baseline_tests = BaselineTests.load_from_issue(issue)
            after_baseline = baseline_tests is not None

            testing_analysis = await _analyze_testing_results(
                issue, related_erratum, gateway_tools, after_baseline=after_baseline
            )

            match testing_analysis.state:
                case TestingState.NOT_RUNNING:
                    state.result = await _flag_attention(
                        issue.key,
                        "Tests aren't running - see details below",
                        details_comment=testing_analysis.comment,
                        tools=gateway_tools,
                        dry_run=state.dry_run,
                    )
                case TestingState.PENDING:
                    state.result = WorkflowResult(
                        status="Tests are pending",
                        reschedule_in=WAIT_DELAY,
                    )
                case TestingState.RUNNING:
                    state.result = WorkflowResult(
                        status="Tests are running",
                        reschedule_in=WAIT_DELAY,
                    )
                case TestingState.FAILED:
                    if testing_analysis.failed_test_ids and (
                        baseline_tests is None
                        or (
                            set(testing_analysis.failed_test_ids)
                            != {c.failed.id for c in baseline_tests.comparisons}
                        )
                    ):
                        # Start reproduction with baseline build
                        previous_build_nvr = await run_tool(
                            "get_erratum_build_nvr",
                            available_tools=gateway_tools,
                            erratum_id=related_erratum.id,
                            component=issue.components[0],
                        )

                        if previous_build_nvr is None:
                            state.result = await _flag_attention(
                                issue.key,
                                "Tests failed - see details below. "
                                "Cannot start reproduction with previous build "
                                "- error finding previous build NVR.",
                                details_comment=testing_analysis.comment,
                                tools=gateway_tools,
                                dry_run=state.dry_run,
                            )
                        else:
                            try:
                                new_baseline_tests = await BaselineTests.create(
                                    failure_comment=testing_analysis.comment or "",
                                    failed_request_ids=testing_analysis.failed_test_ids,
                                    previous_build_nvr=previous_build_nvr,
                                    dry_run=state.dry_run,
                                    tools=gateway_tools,
                                )
                                issue_comment = await new_baseline_tests.format_issue_comment(
                                    tools=gateway_tools
                                )
                                await _add_label(
                                    issue.key,
                                    "ymir_reproducing_tests",
                                    issue_comment,
                                    gateway_tools,
                                    state.dry_run,
                                )
                                state.result = WorkflowResult(
                                    status="Waiting to reproduce tests with previous build",
                                    reschedule_in=WAIT_DELAY,
                                )
                            except Exception as e:
                                logger.exception("Failed to start test reproduction: %s", e)
                                state.result = await _flag_attention(
                                    issue.key,
                                    f"Tests failed - see details below. {e}",
                                    details_comment=testing_analysis.comment,
                                    tools=gateway_tools,
                                    dry_run=state.dry_run,
                                )
                    else:
                        state.result = await _flag_attention(
                            issue.key,
                            "Tests failed - see details below",
                            details_comment=testing_analysis.comment,
                            tools=gateway_tools,
                            dry_run=state.dry_run,
                        )
                case TestingState.ERROR:
                    state.result = await _flag_attention(
                        issue.key,
                        "An error occurred during testing - see details below",
                        details_comment=testing_analysis.comment,
                        tools=gateway_tools,
                        dry_run=state.dry_run,
                    )
                case TestingState.PASSED:
                    state.result = await _change_status(
                        issue.key,
                        issue.status,
                        IssueStatus.RELEASE_PENDING,
                        testing_analysis.comment or "Final testing has passed.",
                        gateway_tools,
                        state.dry_run,
                    )
                case TestingState.WAIVED:
                    state.result = await _change_status(
                        issue.key,
                        issue.status,
                        IssueStatus.RELEASE_PENDING,
                        testing_analysis.comment
                        or "Final testing has been waived, moving to Release Pending.",
                        gateway_tools,
                        state.dry_run,
                    )
                case _:
                    raise ValueError(f"Unknown testing state: {testing_analysis.state}")

            return Workflow.END

        async def check_reproduction(state: IssueVerificationWorkflowState):
            """Check status of baseline test reproduction."""
            issue = state.issue
            baseline_tests = BaselineTests.load_from_issue(issue)

            if baseline_tests is None:
                state.result = await _flag_attention(
                    issue.key,
                    "Issue has ymir_reproducing_tests label but cannot parse baseline tests from comments",
                    tools=gateway_tools,
                    dry_run=state.dry_run,
                )
                return Workflow.END

            if not await baseline_tests.settled(gateway_tools):
                state.result = WorkflowResult(
                    status="Waiting for baseline tests to complete",
                    reschedule_in=WAIT_DELAY,
                )
                return Workflow.END

            await baseline_tests.create_attachments(
                issue_key=issue.key,
                dry_run=state.dry_run,
                tools=gateway_tools,
            )

            issue_comment = await baseline_tests.format_issue_comment(
                include_attachments=True, tools=gateway_tools
            )
            await _remove_label(issue.key, "ymir_reproducing_tests", gateway_tools, state.dry_run)

            # Update the existing comment
            if baseline_tests.comment_id is None:
                raise ValueError("baseline_tests.comment_id must be set")
            if not state.dry_run:
                await run_tool(
                    "update_jira_comment",
                    available_tools=gateway_tools,
                    issue_key=issue.key,
                    comment_id=baseline_tests.comment_id,
                    comment=issue_comment,
                )

            state.result = WorkflowResult(
                status="Baseline tests are complete, will analyze results",
                reschedule_in=0.0,
            )
            return Workflow.END

        workflow.add_step("fetch_and_validate_issue", fetch_and_validate_issue)
        workflow.add_step("check_errata_status", check_errata_status)
        workflow.add_step("run_before_errata", run_before_errata)
        workflow.add_step("run_after_errata", run_after_errata)
        workflow.add_step("analyze_testing", analyze_testing)
        workflow.add_step("check_reproduction", check_reproduction)

        response = await workflow.run(
            IssueVerificationWorkflowState(
                jira_issue=jira_issue,
                dry_run=dry_run,
                ignore_needs_attention=ignore_needs_attention,
            )
        )

        return response.state.result


async def main() -> None:
    configure_logging(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    ignore_needs_attention = os.getenv("IGNORE_NEEDS_ATTENTION", "false").lower() == "true"

    jira_issue = os.getenv("JIRA_ISSUE")
    if not jira_issue:
        logger.error("JIRA_ISSUE environment variable is required")
        sys.exit(1)

    logger.info("Running issue verification for %s (dry_run=%s)", jira_issue, dry_run)
    result = await run_issue_verification(
        jira_issue,
        dry_run=dry_run,
        ignore_needs_attention=ignore_needs_attention,
    )
    logger.info("Completed: status=%s, reschedule_in=%s", result.status, result.reschedule_in)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
