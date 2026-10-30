import asyncio
import json
import logging
import os
import re
import sys
import traceback
from typing import Any

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field

from ymir.agents.observability import setup_observability
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    mcp_tools,
    render_template,
    run_tool,
)
from ymir.common.cve import extract_cve_ids
from ymir.common.logging_setup import configure_logging, get_trajectory_writeable
from ymir.tools.unprivileged.greenwave import FetchTestingFarmResultsTool

logger = logging.getLogger(__file__)

TEST_COVERAGE_CUSTOM_FIELD = "customfield_10638"

# Matches `Resolves: RHEL-12345` (and similar) lines in an MR description,
# mirroring the small regex approach used by mr_consolidation_agent.py rather
# than trying to share its stricter footer-only helpers (which look at commit
# messages, not MR descriptions).
RESOLVES_RE = re.compile(r"Resolves:\s*(RHEL-\d+)", re.IGNORECASE)

_GATEWAY_TOOL_NAMES = [
    "fetch_gitlab_mr_notes",
    "get_authorized_comments_from_merge_request",
    "get_merge_request_details",
    "add_merge_request_comment",
    "update_merge_request_comment",
    "add_blocking_merge_request_comment",
    "add_merge_request_labels",
    "remove_merge_request_labels",
    "add_jira_comment",
    "get_jira_details",
    "parse_mr_test_results",
    "format_mr_testing_comment",
]


class JiraIssueContext(BaseModel):
    issue_key: str
    cve_id: str | None = None
    test_coverage: list[str] = Field(default_factory=list)


class InputSchema(BaseModel):
    mr_url: str = Field(description="URL of the GitLab merge request to analyze")
    mr_description: str = Field(description="Description of the merge request")
    linked_issues: list[JiraIssueContext] = Field(
        description="Jira issues resolved by this MR, with CVE id and Test Coverage already gathered"
    )
    dry_run: bool = Field(description="If true, do not call any posting tools; describe what would be posted")
    poc: bool = Field(
        description=(
            "Proof-of-concept mode: never open a blocking/unresolved MR discussion thread -- "
            "post a plain comment with the same content instead"
        )
    )
    no_jira_comments: bool = Field(
        description="If true, do not post the consolidated summary comment to linked Jira issues"
    )


class MrTestingAnalyzerResult(BaseModel):
    analyzed: bool = Field(description="Whether the MR was analyzed (false on clean bail-out)")
    summary: str = Field(description="Summary of what was posted, or would have been posted in dry_run mode")


def render_prompt(input: InputSchema) -> str:
    return render_template("mr_testing_analyzer/prompt.j2", input)


def create_mr_testing_analyzer_agent(gateway_tools: list) -> ReasoningAgent:
    return ReasoningAgent(
        name="MrTestingAnalyzer",
        description=(
            "Agent that processes Testing Farm results posted as MR comments and posts formatted "
            "test result, CVE verification, and Jira summary comments"
        ),
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            FetchTestingFarmResultsTool(),
        ]
        + [t for t in gateway_tools if t.name in _GATEWAY_TOOL_NAMES],
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
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
    )


class MrTestingAnalyzerWorkflowState(BaseModel):
    mr_url: str
    dry_run: bool = False
    poc: bool = False
    no_jira_comments: bool = False

    mr_description: str | None = Field(default=None)
    linked_issue_keys: list[str] = Field(default_factory=list)
    linked_issues: list[JiraIssueContext] = Field(default_factory=list)
    mr_comments: list[dict[str, Any]] = Field(default_factory=list)

    result: MrTestingAnalyzerResult | None = Field(default=None)


def _extract_linked_issues(mr_description: str) -> list[str]:
    """Extract Jira keys from ``Resolves: RHEL-XXXXX`` lines in an MR description."""
    return sorted({key.upper() for key in RESOLVES_RE.findall(mr_description or "")})


async def run_mr_testing_analyzer(
    mr_url: str,
    dry_run: bool = False,
    poc: bool = False,
    no_jira_comments: bool = False,
) -> MrTestingAnalyzerResult:
    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        workflow = Workflow(MrTestingAnalyzerWorkflowState, name="MrTestingAnalyzerWorkflow")

        async def fetch_and_validate_mr(state: MrTestingAnalyzerWorkflowState):
            """Fetch MR details and extract linked Jira issues from its description."""
            logger.info("Fetching merge request details for %s", state.mr_url)
            mr_details = await run_tool(
                "get_merge_request_details",
                available_tools=gateway_tools,
                merge_request_url=state.mr_url,
            )
            state.mr_description = mr_details.get("description") or ""

            state.linked_issue_keys = _extract_linked_issues(state.mr_description)
            if not state.linked_issue_keys:
                logger.info("No linked Jira issues found on %s, nothing to analyze", state.mr_url)
                state.result = MrTestingAnalyzerResult(
                    analyzed=False,
                    summary=f"No `Resolves: RHEL-XXXXX` line found in {state.mr_url}'s description.",
                )
                return Workflow.END

            logger.info("Linked issues for %s: %s", state.mr_url, state.linked_issue_keys)
            return "fetch_test_results"

        async def fetch_test_results(state: MrTestingAnalyzerWorkflowState):
            """Fetch raw MR comments -- parsing itself is left to the LLM step via
            the parse_mr_test_results tool, mirroring preliminary_testing_agent.py's
            pattern of handing its own local tools to the agent rather than calling
            them directly from a workflow step."""
            try:
                comments_result = await run_tool(
                    "get_authorized_comments_from_merge_request",
                    available_tools=gateway_tools,
                    merge_request_url=state.mr_url,
                )
            except Exception as e:
                logger.warning(
                    "get_authorized_comments_from_merge_request failed for %s, falling back to "
                    "fetch_gitlab_mr_notes: %s",
                    state.mr_url,
                    e,
                )
                comments_result = await run_tool(
                    "fetch_gitlab_mr_notes",
                    available_tools=gateway_tools,
                    merge_request_url=state.mr_url,
                )

            if isinstance(comments_result, dict):
                comments = comments_result.get("comments", [])
            elif isinstance(comments_result, list):
                comments = comments_result
            else:
                comments = []
            state.mr_comments = comments

            return "gather_jira_context"

        async def gather_jira_context(state: MrTestingAnalyzerWorkflowState):
            """Fetch Jira details for each linked issue: CVE id and Test Coverage field."""
            linked_issues = []
            for issue_key in state.linked_issue_keys:
                try:
                    issue_data = await run_tool(
                        "get_jira_details",
                        available_tools=gateway_tools,
                        issue_key=issue_key,
                    )
                except Exception as e:
                    logger.warning("Failed to fetch Jira details for %s: %s", issue_key, e)
                    linked_issues.append(JiraIssueContext(issue_key=issue_key))
                    continue

                fields = issue_data.get("fields", {})
                summary = fields.get("summary", "")
                labels = fields.get("labels", []) or []
                cve_id = extract_cve_ids(summary) or extract_cve_ids(" ".join(labels))

                test_coverage_raw = fields.get(TEST_COVERAGE_CUSTOM_FIELD)
                test_coverage: list[str] = []
                try:
                    test_coverage = [v.get("value") for v in test_coverage_raw if v.get("value")]
                except (TypeError, AttributeError):
                    pass

                linked_issues.append(
                    JiraIssueContext(
                        issue_key=issue_key,
                        cve_id=cve_id,
                        test_coverage=test_coverage,
                    )
                )

            state.linked_issues = linked_issues
            logger.info(
                "Gathered Jira context for %d issue(s): %s",
                len(linked_issues),
                [i.model_dump() for i in linked_issues],
            )

            return "run_analysis"

        async def run_analysis(state: MrTestingAnalyzerWorkflowState):
            """Hand everything gathered so far to the ReasoningAgent, which carries
            out the remaining mr_testing skill steps itself (parsing the MR
            comments, formatting and posting comments, labeling the MR) by calling
            its tools."""
            agent = create_mr_testing_analyzer_agent(gateway_tools)

            input_data = InputSchema(
                mr_url=state.mr_url,
                mr_description=state.mr_description or "",
                linked_issues=state.linked_issues,
                dry_run=state.dry_run,
                poc=state.poc,
                no_jira_comments=state.no_jira_comments,
            )

            prompt = render_prompt(input_data)
            prompt += f"\n\nMR_COMMENTS (JSON, pass as-is to parse_mr_test_results): {json.dumps(state.mr_comments)}"

            response = await agent.run(
                prompt,
                expected_output=MrTestingAnalyzerResult,
                **get_agent_execution_config(),  # type: ignore
            )

            state.result = MrTestingAnalyzerResult.model_validate_json(response.last_message.text)
            logger.info(
                "MR testing analysis completed for %s: %s",
                state.mr_url,
                state.result.model_dump_json(indent=4),
            )

            return "act_on_result"

        async def act_on_result(state: MrTestingAnalyzerWorkflowState):
            """Minimal final step -- all side effects (comments posted, label added)
            already happened inside the ReasoningAgent's tool calls above."""
            logger.info("MR testing analyzer workflow completed for %s", state.mr_url)
            return Workflow.END

        workflow.add_step("fetch_and_validate_mr", fetch_and_validate_mr)
        workflow.add_step("fetch_test_results", fetch_test_results)
        workflow.add_step("gather_jira_context", gather_jira_context)
        workflow.add_step("run_analysis", run_analysis)
        workflow.add_step("act_on_result", act_on_result)

        response = await workflow.run(
            MrTestingAnalyzerWorkflowState(
                mr_url=mr_url,
                dry_run=dry_run,
                poc=poc,
                no_jira_comments=no_jira_comments,
            )
        )

        return response.state.result


async def main() -> None:
    configure_logging(level=logging.INFO)

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    poc = os.getenv("POC", "false").lower() == "true"
    no_jira_comments = os.getenv("NO_JIRA_COMMENTS", "false").lower() == "true"

    mr_url = os.getenv("MR_URL")
    if not mr_url:
        logger.error("MR_URL environment variable is required")
        sys.exit(1)

    logger.info(
        "Running MR testing analysis for %s (dry_run=%s, poc=%s, no_jira_comments=%s)",
        mr_url,
        dry_run,
        poc,
        no_jira_comments,
    )
    with span_processor.start_transaction(mr_url, workflow="MrTestingAnalyzerWorkflow"):
        result = await run_mr_testing_analyzer(
            mr_url,
            dry_run=dry_run,
            poc=poc,
            no_jira_comments=no_jira_comments,
        )
        state_icon = "✅" if result.analyzed else "⚠️"

        separator = "=" * 60
        print(f"\n{separator}")
        print(f"  RESULT: {state_icon}  {'ANALYZED' if result.analyzed else 'SKIPPED'}")
        print(separator)
        if result.summary:
            print(result.summary)
        print(f"{separator}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
