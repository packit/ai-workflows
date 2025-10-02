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
from .tools.read_readme import ReadReadmeTool
from .tools.search_resultsdb import SearchResultsdbTool

logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    issue: FullIssue = Field(description="Details of JIRA issue to analyze")
    test_location_info: TestLocationInfo = Field(
        description="Information about where to find tests and test results"
    )
    erratum: FullErratum | None = Field(description="Details of the related ERRATUM")
    current_time: datetime = Field(description="Current timestamp")


class OutputSchema(BaseModel):
    state: TestingState = Field(description="State of tests")
    comment: str | None = Field(description="Comment to add to the JIRA issue")


def render_prompt(input: InputSchema) -> str:
    template = """
      You are an agent that analyzes a RHEL JIRA issue with a fix attached and determines
      the state and what needs to be done.

      JIRA_ISSUE_DATA: {{ issue }}
      ERRATUM_DATA: {{ erratum }}
      TEST_LOCATION_INFO: {{ test_location_info }}
      CURRENT_TIME: {{ current_time }}

      For components handled by the New Errata Workflow Automation(NEWA):
      NEWA will post a comment to the erratum when it has started tests and when they finish.
      Read the JIRA issue in those comments to find test results.

      Call the final_answer tool passing in the state and a comment as follows.
      The comment should use JIRA comment syntax.

      If the tests need to be started manually:
         state: tests-not-running
         comment: [explain what needs to be done to start tests]

      If the tests are complete and failed:
         state: tests-failed
         comment: [list failed tests with URLs]

      If the tests are complete and passed:
         state: tests-passed
         comment: [Give a brief summary of what was tested with a link to the result.]

      If the tests will be started automatically without user intervention, but are not yet running:
         state: tests-pending
         comment: [Provide a brief description of what tests are expected to run and where the results will be]

      If the tests are currently running:
         state: tests-running
         comment: [Provide a brief description of what tests are running and where the results will be]
    """
    return PromptTemplate(
        PromptTemplateInput(schema=InputSchema, template=template)
    ).render(input)


async def analyze_issue(jira_issue: FullIssue, erratum: FullErratum | None) -> OutputSchema:
    agent = ToolCallingAgent(
        llm=ChatModel.from_name(
            os.environ["CHAT_MODEL"],
            allow_parallel_tool_calls=True,
        ),
        memory=UnconstrainedMemory(),
        tools=[ReadReadmeTool(), SearchResultsdbTool()],
    )

    async def run(input: InputSchema):
        response = await agent.run(
            render_prompt(input),
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
