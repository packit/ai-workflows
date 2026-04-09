import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.agents.tool_calling import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend import ChatModel
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.template import PromptTemplate, PromptTemplateInput

from agents.utils import get_agent_execution_config
from .supervisor_types import FullIssue, TestingState
from .tools.fetch_greenwave import FetchGreenWaveTool
from .tools.fetch_gitlab_mr_notes import FetchGitlabMrNotesTool
from .tools.read_issue import ReadIssueTool

logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    issue: FullIssue = Field(description="Details of JIRA issue to analyze")
    build_nvr: str | None = Field(description="NVR of the build to check, if available")
    jira_pull_requests: str = Field(
        description="Pull/merge requests linked in Jira Development section (JSON)"
    )
    current_time: datetime = Field(description="Current timestamp")


class PreliminaryTestingResult(BaseModel):
    state: TestingState = Field(description="State of preliminary testing")
    comment: str | None = Field(
        description="Comment to add to the JIRA issue explaining the result"
    )


TEMPLATE = """\
You are the preliminary testing analyst agent for Project Jötnar. Your task is to
analyze a RHEL JIRA issue and determine if the build fixing it has passed preliminary
testing — the gating and CI checks that must pass before the build can be added to a
compose and erratum.

JIRA_ISSUE_DATA: {{ issue }}
BUILD_NVR: {{ build_nvr }}
JIRA_PULL_REQUESTS (from Jira Development section): {{ jira_pull_requests }}
CURRENT_TIME: {{ current_time }}

You have two sources of test results to check. You should attempt to check all
available sources, and make your decision based on whichever results you can obtain.

1. **GreenWave / OSCI Gating Status**: If BUILD_NVR is available (not None), use
   the fetch_greenwave tool with the BUILD_NVR to check the OSCI gating results.
   The HTML page will show which gating test jobs ran and whether they passed or
   failed. All required/gating tests must pass.
   The GreenWave Monitor URL is https://gating-status.osci.redhat.com/query?nvr=BUILD_NVR
   — when linking to gating results in your comment, ONLY use this exact URL pattern.
   Do NOT invent or guess any other URLs for gating results.
   If BUILD_NVR is None, skip this source.

2. **OSCI results in MR comments**: If JIRA_PULL_REQUESTS contains linked merge
   requests (from the Jira Development section), use the fetch_gitlab_mr_notes tool
   to read the comments on those MRs. Look for comments titled "Results for pipeline ..."
   — these contain OSCI test results. Parse these results to determine which tests
   passed and which failed.
   To use fetch_gitlab_mr_notes, extract the project path and MR IID from the
   JIRA_PULL_REQUESTS data. The "id" field has format "project/path!iid" and the
   "url" field contains the full MR URL. The "repositoryUrl" contains the project URL
   from which you can derive the project path (remove the leading https://gitlab.com/).

If a tool call fails or returns an error, note it in your comment but continue
analyzing with the results you were able to obtain. Only return tests-error if
you could not obtain results from ANY source.

Call the final_answer tool passing in the state and a comment as follows.
The comment should use JIRA comment syntax (headings, bullet points, links).
Do NOT wrap your comment in a {{panel}} macro — that will be added automatically.

If all available gating tests have passed (and MR OSCI results passed, if available):
    state: tests-passed
    comment: [Brief summary of what passed, with links to the GreenWave page and MR
              if available. Note if any source was unavailable.]

If any required/gating tests have failed:
    state: tests-failed
    comment: [List the failed tests with URLs, explain which are from GreenWave and
              which from MR comments]

If tests are still running (pipeline status is running, or GreenWave shows tests in progress):
    state: tests-running
    comment: [Brief description of what is still running]

If tests are queued but not yet started:
    state: tests-pending
    comment: [Brief description]

If no test results can be found from any source:
    state: tests-not-running
    comment: [Explain that no test results were found and manual intervention may be needed]

If all sources returned errors and no results could be obtained:
    state: tests-error
    comment: [Explain which sources were tried and what errors occurred]
"""


def render_prompt(input: InputSchema) -> str:
    return PromptTemplate(
        PromptTemplateInput(schema=InputSchema, template=TEMPLATE)
    ).render(input)


async def analyze_preliminary_testing(
    jira_issue: FullIssue,
    build_nvr: str | None,
    jira_pull_requests: list[dict[str, Any]] | None = None,
) -> PreliminaryTestingResult:
    tools = [
        FetchGreenWaveTool(),
        FetchGitlabMrNotesTool(),
        ReadIssueTool(),
    ]

    agent = ToolCallingAgent(
        llm=ChatModel.from_name(
            os.environ["CHAT_MODEL"],
            allow_parallel_tool_calls=True,
        ),
        memory=UnconstrainedMemory(),
        tools=tools,
        meta=AgentMeta(
            name="PreliminaryTestingAnalyst",
            description="Agent that analyzes GreenWave gating and MR comment results to determine preliminary testing status",
            tools=tools,
        ),
    )

    input = InputSchema(
        issue=jira_issue,
        build_nvr=build_nvr,
        jira_pull_requests=json.dumps(jira_pull_requests or [], indent=2),
        current_time=datetime.now(timezone.utc),
    )

    response = await agent.run(
        render_prompt(input),
        expected_output=PreliminaryTestingResult,
        **get_agent_execution_config(),  # type: ignore
    )

    if response.state.result is None:
        raise ValueError("Agent did not return a result")

    output = PreliminaryTestingResult.model_validate_json(response.state.result.text)
    logger.info(
        "Preliminary testing analysis completed: %s", output.model_dump_json(indent=4)
    )
    return output
