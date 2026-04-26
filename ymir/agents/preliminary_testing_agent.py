import asyncio
import json
import logging
import os
import sys
import traceback
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.template import PromptTemplate, PromptTemplateInput
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field

from ymir.agents.observability import setup_observability
from ymir.agents.utils import (
    enable_prompt_caching,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    mcp_tools,
    run_tool,
)
from ymir.tools.unprivileged.greenwave import FetchGreenWaveTool

logger = logging.getLogger(__name__)

FIXED_IN_BUILD_CUSTOM_FIELD = "customfield_10578"
TEST_COVERAGE_CUSTOM_FIELD = "customfield_10638"
PRELIMINARY_TESTING_CUSTOM_FIELD = "customfield_10879"


class TestingState(StrEnum):
    NOT_RUNNING = "tests-not-running"
    PENDING = "tests-pending"
    RUNNING = "tests-running"
    ERROR = "tests-error"
    FAILED = "tests-failed"
    PASSED = "tests-passed"
    WAIVED = "tests-waived"


class InputSchema(BaseModel):
    issue_key: str = Field(description="JIRA issue key")
    issue_data: str = Field(description="JSON representation of the JIRA issue details")
    build_nvr: str | None = Field(description="NVR of the build to check, if available")
    jira_pull_requests: str = Field(
        description="Pull/merge requests linked in Jira Development section (JSON)"
    )
    current_time: datetime = Field(description="Current timestamp")


class PreliminaryTestingResult(BaseModel):
    state: TestingState = Field(description="State of preliminary testing")
    comment: str | None = Field(description="Comment to add to the JIRA issue explaining the result")


TEMPLATE = """\
You are the preliminary testing analyst agent for Project Ymir. Your task is to
analyze a RHEL JIRA issue and determine if the build fixing it has passed preliminary
testing — the gating and CI checks that must pass before the build can be added to a
compose and erratum.

JIRA_ISSUE_KEY: {{ issue_key }}
JIRA_ISSUE_DATA: {{ issue_data }}
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
    return PromptTemplate(PromptTemplateInput(schema=InputSchema, template=TEMPLATE)).render(input)


def create_preliminary_testing_agent(gateway_tools: list) -> RequirementAgent:
    return RequirementAgent(
        name="PreliminaryTestingAnalyst",
        description=(
            "Agent that analyzes GreenWave gating and MR comment results"
            " to determine preliminary testing status"
        ),
        llm=get_chat_model(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            FetchGreenWaveTool(),
        ]
        + [
            t
            for t in gateway_tools
            if t.name
            in [
                "fetch_gitlab_mr_notes",
                "get_jira_details",
            ]
        ],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                force_after=Tool,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
    )


ATTENTION_TEMPLATE = (
    "{{panel:title=Project Ymir: ATTENTION NEEDED|"
    "borderStyle=solid|borderColor=#CC0000|titleBGColor=#FFF5F5|bgColor=#FFFEF0}}\n"
    "{why}\n\n"
    "Please resolve this and remove the {{ymir_needs_attention}} flag.\n"
    "{{panel}}"
)


class PreliminaryTestingWorkflowState(BaseModel):
    jira_issue: str
    dry_run: bool = False
    ignore_needs_attention: bool = False

    issue_data: dict[str, Any] | None = Field(default=None)
    build_nvr: str | None = Field(default=None)
    pull_requests: list[dict[str, Any]] = Field(default_factory=list)
    test_coverage_missing: bool = Field(default=True)

    result: PreliminaryTestingResult | None = Field(default=None)


async def run_preliminary_testing(
    jira_issue: str,
    dry_run: bool = False,
    ignore_needs_attention: bool = False,
) -> PreliminaryTestingResult:
    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        workflow = Workflow(PreliminaryTestingWorkflowState, name="PreliminaryTestingWorkflow")

        async def fetch_and_validate_issue(state: PreliminaryTestingWorkflowState):
            """Fetch JIRA issue data and validate preconditions."""
            logger.info("Fetching JIRA issue data for %s", state.jira_issue)
            state.issue_data = await run_tool(
                "get_jira_details",
                available_tools=gateway_tools,
                issue_key=state.jira_issue,
            )

            fields = state.issue_data.get("fields", {})
            status = fields.get("status", {}).get("name", "")
            labels = fields.get("labels", [])

            needs_attention_label = "ymir_needs_attention"
            if needs_attention_label in labels and not state.ignore_needs_attention:
                logger.info(
                    "Issue %s has %s label, skipping",
                    state.jira_issue,
                    needs_attention_label,
                )
                state.result = PreliminaryTestingResult(
                    state=TestingState.ERROR,
                    comment=f"Issue has the {needs_attention_label} label",
                )
                return Workflow.END

            components = [c["name"] for c in fields.get("components", [])]
            if len(components) != 1:
                logger.warning(
                    "Issue %s has %d components, expected 1",
                    state.jira_issue,
                    len(components),
                )
                await _flag_attention(
                    state.jira_issue,
                    "This issue has multiple components. This workflow expects exactly one component.",
                    gateway_tools=gateway_tools,
                )
                state.result = PreliminaryTestingResult(
                    state=TestingState.ERROR,
                    comment="Issue has multiple components",
                )
                return Workflow.END

            if status != "In Progress":
                logger.info(
                    "Issue %s status is %s, expected In Progress",
                    state.jira_issue,
                    status,
                )
                state.result = PreliminaryTestingResult(
                    state=TestingState.ERROR,
                    comment=f"Issue status is {status}, expected In Progress",
                )
                return Workflow.END

            # Check Preliminary Testing field
            preliminary_testing = fields.get(PRELIMINARY_TESTING_CUSTOM_FIELD)
            preliminary_testing_value = (
                preliminary_testing.get("value") if isinstance(preliminary_testing, dict) else None
            )

            if preliminary_testing_value == "Pass":
                logger.info("Issue %s Preliminary Testing is already Pass", state.jira_issue)
                state.result = PreliminaryTestingResult(
                    state=TestingState.PASSED,
                    comment="Preliminary Testing is already set to Pass",
                )
                return Workflow.END

            return "gather_test_sources"

        async def gather_test_sources(state: PreliminaryTestingWorkflowState):
            """Gather build NVR, pull requests, and test coverage info."""
            fields = state.issue_data.get("fields", {})

            # Check Test Coverage
            test_coverage = fields.get(TEST_COVERAGE_CUSTOM_FIELD)
            try:
                if any(
                    v.get("value") in ("Manual", "Automated", "RegressionOnly", "New Test Coverage")
                    for v in test_coverage
                ):
                    state.test_coverage_missing = False
            except (TypeError, AttributeError):
                pass

            state.build_nvr = fields.get(FIXED_IN_BUILD_CUSTOM_FIELD)

            try:
                state.pull_requests = await run_tool(
                    "get_jira_pull_requests",
                    available_tools=gateway_tools,
                    issue_key=state.jira_issue,
                )
            except Exception as e:
                logger.warning("Failed to get pull requests for %s: %s", state.jira_issue, e)

            if state.pull_requests:
                logger.info(
                    "Found %d pull request(s) for %s",
                    len(state.pull_requests),
                    state.jira_issue,
                )
            else:
                logger.warning("No pull requests found for %s", state.jira_issue)

            if state.build_nvr is None and not state.pull_requests:
                logger.info("Issue %s has no build NVR and no linked PRs", state.jira_issue)
                state.result = PreliminaryTestingResult(
                    state=TestingState.ERROR,
                    comment="Issue has no Fixed in Build and no linked pull requests",
                )
                return Workflow.END

            if state.build_nvr is None:
                logger.info(
                    "Fixed in Build not set for %s, will analyze using MR results only",
                    state.jira_issue,
                )

            return "run_analysis"

        async def run_analysis(state: PreliminaryTestingWorkflowState):
            """Run AI agent to analyze test results."""
            agent = create_preliminary_testing_agent(gateway_tools)

            input_data = InputSchema(
                issue_key=state.jira_issue,
                issue_data=json.dumps(state.issue_data, indent=2, default=str),
                build_nvr=state.build_nvr,
                jira_pull_requests=json.dumps(state.pull_requests, indent=2),
                current_time=datetime.now(UTC),
            )

            response = await agent.run(
                render_prompt(input_data),
                expected_output=PreliminaryTestingResult,
                **get_agent_execution_config(),  # type: ignore
            )

            state.result = PreliminaryTestingResult.model_validate_json(response.last_message.text)
            logger.info(
                "Preliminary testing analysis completed: %s",
                state.result.model_dump_json(indent=4),
            )

            return "act_on_result"

        async def act_on_result(state: PreliminaryTestingWorkflowState):
            """Act on the analysis result: update JIRA fields or flag attention."""
            match state.result.state:
                case TestingState.PASSED | TestingState.WAIVED:
                    if state.test_coverage_missing:
                        await _flag_attention(
                            state.jira_issue,
                            "Preliminary tests passed but Test Coverage field is not set",
                            details_comment=state.result.comment,
                            gateway_tools=gateway_tools,
                        )
                    else:
                        comment = state.result.comment or "Preliminary testing has passed."
                        await run_tool(
                            "set_preliminary_testing",
                            available_tools=gateway_tools,
                            issue_key=state.jira_issue,
                            value="Pass",
                            comment=comment,
                        )
                case TestingState.FAILED:
                    await _flag_attention(
                        state.jira_issue,
                        "Preliminary testing failed - see details below",
                        details_comment=state.result.comment,
                        gateway_tools=gateway_tools,
                    )
                case TestingState.PENDING | TestingState.RUNNING:
                    logger.info(
                        "Tests are %s for %s, no action taken",
                        state.result.state,
                        state.jira_issue,
                    )
                case TestingState.NOT_RUNNING:
                    await _flag_attention(
                        state.jira_issue,
                        "Preliminary tests are not running - see details below",
                        details_comment=state.result.comment,
                        gateway_tools=gateway_tools,
                    )
                case TestingState.ERROR:
                    await _flag_attention(
                        state.jira_issue,
                        "An error occurred during preliminary testing analysis - see details below",
                        details_comment=state.result.comment,
                        gateway_tools=gateway_tools,
                    )

            return Workflow.END

        workflow.add_step("fetch_and_validate_issue", fetch_and_validate_issue)
        workflow.add_step("gather_test_sources", gather_test_sources)
        workflow.add_step("run_analysis", run_analysis)
        workflow.add_step("act_on_result", act_on_result)

        response = await workflow.run(
            PreliminaryTestingWorkflowState(
                jira_issue=jira_issue,
                dry_run=dry_run,
                ignore_needs_attention=ignore_needs_attention,
            )
        )

        return response.state.result


async def _flag_attention(
    issue_key: str,
    why: str,
    *,
    details_comment: str | None = None,
    gateway_tools: list,
) -> None:
    full_comment = ATTENTION_TEMPLATE.format(why=why)
    if details_comment:
        full_comment = f"{full_comment}\n\n{details_comment}"

    await run_tool(
        "edit_jira_labels",
        available_tools=gateway_tools,
        issue_key=issue_key,
        labels_to_add=["ymir_needs_attention"],
    )
    await run_tool(
        "add_jira_comment",
        available_tools=gateway_tools,
        issue_key=issue_key,
        comment=full_comment,
        private=True,
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])
    enable_prompt_caching()

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    ignore_needs_attention = os.getenv("IGNORE_NEEDS_ATTENTION", "false").lower() == "true"

    jira_issue = os.getenv("JIRA_ISSUE")
    if not jira_issue:
        logger.error("JIRA_ISSUE environment variable is required")
        sys.exit(1)

    logger.info("Running preliminary testing analysis for %s (dry_run=%s)", jira_issue, dry_run)
    result = await run_preliminary_testing(
        jira_issue,
        dry_run=dry_run,
        ignore_needs_attention=ignore_needs_attention,
    )
    logger.info("Completed: state=%s", result.state)
    if result.comment:
        logger.info("Comment: %s", result.comment)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
