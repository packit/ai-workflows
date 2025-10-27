import logging
import os
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from beeai_framework.agents.tool_calling import ToolCallingAgent
from beeai_framework.backend import ChatModel
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.template import PromptTemplate, PromptTemplateInput

from agents.utils import get_agent_execution_config
from .qe_data import get_qe_data, TestLocationInfo
from .supervisor_types import FullErratum, FullIssue, TestingState
from .tools.read_attachment import ReadAttachmentTool
from .tools.read_readme import ReadReadmeTool
from .tools.read_issue import ReadIssueTool
from .tools.read_logfile import ReadLogfileTool
from .tools.search_resultsdb import SearchResultsdbTool

logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    issue: FullIssue = Field(description="Details of JIRA issue to analyze")
    test_location_info: TestLocationInfo = Field(
        description="Information about where to find tests and test results"
    )
    erratum: FullErratum = Field(description="Details of the related ERRATUM")
    current_time: datetime = Field(description="Current timestamp")


class OutputSchema(BaseModel):
    state: TestingState = Field(description="State of tests")
    comment: str | None = Field(description="Comment to add to the JIRA issue")
    failed_test_ids: list[str] | None = Field(
        description="List of Testing Farm run IDs with failures"
    )


TEMPLATE_COMMON = """\
You are the testing analyst agent for Project Jötnar. Comments that tag
[~jotnar-project] in JIRA issues are directed to you and other Jötnar agents
sharing the same account—pay close attention to these.

Your task is to analyze a RHEL JIRA issue with a fix attached and determine
the state of testing and what needs to be done.

JIRA_ISSUE_DATA: {{ issue }}
ERRATUM_DATA: {{ erratum }}
TEST_LOCATION_INFO: {{ test_location_info }}
CURRENT_TIME: {{ current_time }}
"""

TEMPLATE_NORMAL = (
    TEMPLATE_COMMON
    + """
For components handled by the New Errata Workflow Automation(NEWA):
NEWA will post a comment to the erratum when it has started tests and when they finish.
Read the JIRA issue in those comments to find test results. Ignore any comments
with links to TCMS or Beaker; older EWA automation may have run in parallel
with NEWA, but should be ignored.

You cannot assume that tests have passed just because a comment says they have
finished, it is mandatory to check the actual test results in the JIRA issue.
Make sure that the JIRA issue is the correct issue for the latest build in the
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

TEMPLATE_AFTER_BASELINE = (
    TEMPLATE_COMMON
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

If the tests failures seem to reflect a regression in the new build compared to the baseline:
    state: tests-failed
    comment: detailed description of the tests that are failing, and if known, possible reasons why

If you are uncertain whether the failures are due to a regression or not:
    state: tests-failed
    comment: detailed description about what might reflect a regression

If it seems highly likely that there are no regressions:
    state: tests-waived
    comment: description of why the failures are not likely to be regressions
"""
)


def render_prompt(input: InputSchema, after_baseline: bool) -> str:

    template = TEMPLATE_AFTER_BASELINE if after_baseline else TEMPLATE_NORMAL
    return PromptTemplate(
        PromptTemplateInput(schema=InputSchema, template=template)
    ).render(input)


async def analyze_issue(
    jira_issue: FullIssue, erratum: FullErratum, after_baseline=False
) -> OutputSchema:
    agent = ToolCallingAgent(
        llm=ChatModel.from_name(
            os.environ["CHAT_MODEL"],
            allow_parallel_tool_calls=True,
        ),
        memory=UnconstrainedMemory(),
        tools=[
            ReadAttachmentTool(),
            ReadLogfileTool(),
            ReadReadmeTool(),
            ReadIssueTool(),
            SearchResultsdbTool(),
        ],
    )

    async def run(input: InputSchema):
        response = await agent.run(
            render_prompt(input, after_baseline=after_baseline),
            expected_output=OutputSchema,
            **get_agent_execution_config(),  # type: ignore
        )
        if response.state.result is None:
            raise ValueError("Agent did not return a result")
        return OutputSchema.model_validate_json(response.state.result.text)

    output = await run(
        InputSchema(
            issue=jira_issue,
            test_location_info=await get_qe_data(jira_issue.components[0]),
            erratum=erratum,
            current_time=datetime.now(timezone.utc),
        )
    )
    logger.info(f"Direct run completed: {output.model_dump_json(indent=4)}")
    return output
