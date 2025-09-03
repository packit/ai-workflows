import asyncio
from functools import cache
import logging
import os

import aiohttp
from pydantic import BaseModel, Field

from beeai_framework.agents.tool_calling import ToolCallingAgent
from beeai_framework.backend import ChatModel
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.template import PromptTemplate, PromptTemplateInput

from agents.utils import get_agent_execution_config
from .supervisor_types import FullIssue, TestingState
from .tools.read_readme import ReadReadmeTool

logger = logging.getLogger(__name__)


class TestLocationInfo(BaseModel):
    assigned_team: str
    component: str
    qa_contact: str
    tests_location: str | None = None
    test_config_location: str | None = None
    test_trigger_method: str | None = None
    test_result_location: str | None = None
    test_docs_url: str | None = None
    notes: str | None = None


class InputSchema(BaseModel):
    issue: FullIssue = Field(description="Details of JIRA issue to analyze")
    test_location_info: TestLocationInfo = Field(
        description="Information about where to find tests and test results"
    )


class OutputSchema(BaseModel):
    state: TestingState = Field(description="State of tests")
    comment: str | None = Field(description="Comment to add to the JIRA issue")


async def fetch_qe_data_map() -> dict[str, dict[str, str]]:
    url = "https://gitlab.cee.redhat.com/otaylor/jotnar-qe-data/-/raw/main/jotnar_qe_data.json?ref_type=heads&inline=false"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


@cache
def get_qe_data_map_task() -> asyncio.Task[dict[str, dict[str, str]]]:
    return asyncio.create_task(fetch_qe_data_map())


async def get_qe_data(component: str) -> TestLocationInfo:
    return TestLocationInfo(**(await get_qe_data_map_task()).get(component, {}))


def render_prompt(input: InputSchema) -> str:
    template = """
      You are an agent that analyzes a RHEL JIRA issue with a fix attached and determines
      the state and what needs to be done.

      JIRA_ISSUE_DATA: {{ issue }}
      TEST_LOCATION_INFO: {{ test_location_info }}

      Call the final_answer tool passing in the state and a comment as follows.
      The comment should use JIRA comment syntax.

      If the tests need to be started manually:
         state: tests-not-running
         comment: [explain what needs to be done to start tests]

      If the tests are complete and failed:
         state: tests-failed:
         comment: [list failed tests with URLs.t]

      If the tests are complete and passed:
         state: tests-passed:
         comment: [Give a brief summary of what was tested with a link to the result.]

      If the tests will be started automatically without user intervention, but are not yet running::
         state: tests-pending
         commnet: [Provide a brief description of what tests are expected to run and where the results will be]

      If the tests are currently running:
         state: tests-running
         comment: [Provide a brief description of what tests are running and where the results will be]
    """
    return PromptTemplate(
        PromptTemplateInput(schema=InputSchema, template=template)
    ).render(input)


async def analyze_issue(jira_issue: FullIssue) -> OutputSchema:
    agent = ToolCallingAgent(
        llm=ChatModel.from_name(
            os.environ["CHAT_MODEL"],
            allow_parallel_tools_calls=True,
        ),
        memory=UnconstrainedMemory(),
        tools=[ReadReadmeTool()],
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
        )
    )
    logger.info(f"Direct run completed: {output.model_dump_json(indent=4)}")
    return output
