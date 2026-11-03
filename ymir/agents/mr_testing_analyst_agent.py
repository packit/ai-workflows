import asyncio
import json
import logging
import os
import re
import sys
import traceback
from typing import Any, Literal

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
MR_TESTING_ANALYZED_LABEL = "mr_testing_analyzed"
MR_TESTING_COMPLETED_LABEL = "mr_testing_completed"

# Matches `Resolves: RHEL-12345` (and similar) lines in an MR description,
# mirroring the small regex approach used by mr_consolidation_agent.py rather
# than trying to share its stricter footer-only helpers (which look at commit
# messages, not MR descriptions).
RESOLVES_RE = re.compile(r"Resolves:\s*(RHEL-\d+)", re.IGNORECASE)

# Matches a GitLab MR URL into (project_path, iid), same pattern gitlab.py's
# own (private) _get_merge_request_from_url uses -- needed here because
# fetch_gitlab_mr_notes takes project/mr_iid separately, unlike every other
# MR-related gateway tool used in this agent, which all take merge_request_url.
_MR_URL_RE = re.compile(r"gitlab\.com/([^/]+(?:/[^/]+){1,3})/-/merge_requests/(\d+)")

# The ReasoningAgent only ever reads data and builds comment content with
# these -- it never calls a tool that writes to GitLab/Jira itself. All
# posting (comments, labels, Jira comments, discussion resolution) happens
# deterministically in act_on_result() below, driven by the structured
# content/decisions this agent returns in MrTestingAnalystResult. This keeps
# dry_run/poc/no_jira_comments out of the agent's prompt entirely -- those
# are posting-time concerns Python handles, not analysis-time ones.
_GATEWAY_TOOL_NAMES = [
    "fetch_gitlab_mr_notes",
    "get_authorized_comments_from_merge_request",
    "get_merge_request_details",
    "get_jira_details",
    "parse_mr_test_results",
    "format_mr_testing_comment",
    "fold_mr_comment_body",
    # NEWA support
    "parse_newa_mr_comment",
    # Gating mode (OSCI bot pipeline results, no anewa/NEWA run)
    "parse_osci_results_comment",
    "get_pipeline_job_trace",
    "extract_tf_request_from_job_trace",
    "get_merge_request_discussions",
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
    gating: bool = Field(
        default=False,
        description=(
            "Gating mode: source results from the MR's latest OSCI bot pipeline comment (the "
            "required gating pipeline every MR runs) instead of an anewa/NEWA comment. Produces a "
            "briefer results comment, and this agent should propose resolving OSCI bot's own "
            "results discussion thread (via resolve_discussion_id below) only when a failure is "
            "judged to be bad infrastructure or a broken test, not a real regression."
        ),
    )


class MrCommentToPost(BaseModel):
    kind: Literal["new", "update", "blocking"] = Field(
        description=(
            "'new': post a plain new comment. "
            "'update': replace an existing comment in place (note_id required; body must be the "
            "output of fold_mr_comment_body, not hand-composed) -- used only to fold the Testing "
            "Farm Results summary into anewa's own completed comment. "
            "'blocking': this needs explicit human sign-off (Untested Issues / Test Failure "
            "Analysis) -- posted as an unresolved discussion thread, or a plain comment in poc "
            "mode; which one is Python's decision, not yours."
        )
    )
    body: str = Field(
        description="Fully formatted comment markdown, built via format_mr_testing_comment "
        "(or fold_mr_comment_body for kind='update') -- never hand-composed"
    )
    note_id: int | None = Field(
        default=None, description="Required when kind == 'update': the note ID to update in place"
    )


class JiraCommentToPost(BaseModel):
    jira_key: str = Field(description="Jira issue key this comment is posted to")
    body: str = Field(description="Fully formatted comment markdown, built via format_mr_testing_comment")


class MrTestingAnalystResult(BaseModel):
    analyzed: bool = Field(
        description="Whether this MR had anewa/NEWA/OSCI results ready to analyze (false if "
        "testing hasn't finished yet, or no results were found at all)"
    )
    summary: str = Field(description="Concise description of the analysis and what is being posted")
    mr_comments: list[MrCommentToPost] = Field(
        default_factory=list, description="MR comments to post, in the order they should be posted"
    )
    jira_comments: list[JiraCommentToPost] = Field(
        default_factory=list, description="One consolidated summary comment per linked Jira issue"
    )
    resolve_discussion_id: str | None = Field(
        default=None,
        description="Gating mode only: the discussion_id of OSCI bot's own results thread to "
        "resolve, if every failed/errored gating test job's assessment is exactly 'Infrastructure "
        "issue' or 'Test bug'. Null otherwise -- including when gating is false, when nothing "
        "failed (OSCI bot resolves its own thread on a clean run), or when any failure needs a "
        "human to review first.",
    )


def render_prompt(input: InputSchema) -> str:
    return render_template("mr_testing_analyst/prompt.j2", input)


def create_mr_testing_analyst_agent(gateway_tools: list) -> ReasoningAgent:
    return ReasoningAgent(
        name="MrTestingAnalyst",
        description=(
            "Agent that processes Testing Farm results posted as MR comments and builds formatted "
            "test result, CVE verification, and Jira summary comment content for deterministic posting"
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


class MrTestingAnalystWorkflowState(BaseModel):
    mr_url: str
    dry_run: bool = False
    poc: bool = False
    no_jira_comments: bool = False
    gating: bool = False

    mr_description: str | None = Field(default=None)
    linked_issue_keys: list[str] = Field(default_factory=list)
    linked_issues: list[JiraIssueContext] = Field(default_factory=list)
    mr_comments: list[dict[str, Any]] = Field(default_factory=list)

    result: MrTestingAnalystResult | None = Field(default=None)


def _extract_linked_issues(mr_description: str) -> list[str]:
    """Extract Jira keys from ``Resolves: RHEL-XXXXX`` lines in an MR description."""
    return sorted({key.upper() for key in RESOLVES_RE.findall(mr_description or "")})


def _parse_mr_url(mr_url: str) -> tuple[str, int]:
    """Parse a GitLab MR URL into (project_path, iid), for fetch_gitlab_mr_notes'
    project/mr_iid input -- the one gateway tool in this agent that doesn't
    take merge_request_url directly."""
    match = _MR_URL_RE.search(mr_url)
    if not match:
        raise ValueError(f"Could not parse merge request URL: {mr_url}")
    return match.group(1), int(match.group(2))


async def _mark_mr_analyzed(mr_url: str, gateway_tools: list, dry_run: bool) -> None:
    """Apply the mr_testing_completed -> mr_testing_analyzed label transition.
    Never called in gating mode (gating never labels the MR -- that lifecycle
    belongs to anewa alone)."""
    if dry_run:
        logger.info(
            "[DRY RUN] Would label %s as %s and remove %s",
            mr_url,
            MR_TESTING_ANALYZED_LABEL,
            MR_TESTING_COMPLETED_LABEL,
        )
        return
    await run_tool(
        "add_merge_request_labels",
        available_tools=gateway_tools,
        merge_request_url=mr_url,
        labels=[MR_TESTING_ANALYZED_LABEL],
    )
    await run_tool(
        "remove_merge_request_labels",
        available_tools=gateway_tools,
        merge_request_url=mr_url,
        labels=[MR_TESTING_COMPLETED_LABEL],
    )


async def run_mr_testing_analyst(
    mr_url: str,
    dry_run: bool = False,
    poc: bool = False,
    no_jira_comments: bool = False,
    gating: bool = False,
) -> MrTestingAnalystResult:
    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        workflow = Workflow(MrTestingAnalystWorkflowState, name="MrTestingAnalystWorkflow")

        async def fetch_and_validate_mr(state: MrTestingAnalystWorkflowState):
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
                # This MR will never have anything for this agent to analyze,
                # so (outside gating mode, which never labels) advance its
                # label past mr_testing_completed here -- otherwise a
                # scheduler polling on that label would keep re-queuing this
                # MR for analysis forever.
                if not state.gating:
                    await _mark_mr_analyzed(state.mr_url, gateway_tools, state.dry_run)
                state.result = MrTestingAnalystResult(
                    analyzed=False,
                    summary=f"No `Resolves: RHEL-XXXXX` line found in {state.mr_url}'s description.",
                )
                return Workflow.END

            logger.info("Linked issues for %s: %s", state.mr_url, state.linked_issue_keys)
            return "fetch_test_results"

        async def fetch_test_results(state: MrTestingAnalystWorkflowState):
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
                # Unlike every other MR-related gateway tool used in this
                # agent, fetch_gitlab_mr_notes takes project/mr_iid rather
                # than merge_request_url, and returns its result as a JSON
                # string body (StringToolOutput), not structured JSON --
                # both need handling here that the dict/list branches below
                # don't otherwise apply.
                project, mr_iid = _parse_mr_url(state.mr_url)
                comments_result = await run_tool(
                    "fetch_gitlab_mr_notes",
                    available_tools=gateway_tools,
                    project=project,
                    mr_iid=mr_iid,
                )
                if isinstance(comments_result, str):
                    comments_result = json.loads(comments_result)

            if isinstance(comments_result, dict):
                comments = comments_result.get("comments", [])
            elif isinstance(comments_result, list):
                comments = comments_result
            else:
                comments = []
            state.mr_comments = comments

            return "gather_jira_context"

        async def gather_jira_context(state: MrTestingAnalystWorkflowState):
            """Fetch Jira details for each linked issue: CVE id and Test Coverage field.

            A fetch failure here aborts the whole run (analyzed: false, nothing
            posted or labeled) rather than silently substituting an empty
            CVE/Test Coverage context and continuing -- proceeding on missing
            data could misclassify an issue as untested, or wrongly flag the
            MR as analyzed, on what might just be a transient Jira outage.
            """
            linked_issues = []
            for issue_key in state.linked_issue_keys:
                try:
                    issue_data = await run_tool(
                        "get_jira_details",
                        available_tools=gateway_tools,
                        issue_key=issue_key,
                    )
                except Exception as e:
                    logger.warning("Failed to fetch Jira details for %s, aborting run: %s", issue_key, e)
                    state.result = MrTestingAnalystResult(
                        analyzed=False,
                        summary=f"Failed to fetch Jira details for {issue_key}: {e}. Aborting without "
                        "posting or labeling so this can be retried once Jira is reachable again.",
                    )
                    return Workflow.END

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

        async def run_analysis(state: MrTestingAnalystWorkflowState):
            """Hand everything gathered so far to the ReasoningAgent, which parses
            the MR comments and builds the formatted comment content and posting
            decisions (but never posts anything itself -- see act_on_result)."""
            agent = create_mr_testing_analyst_agent(gateway_tools)

            input_data = InputSchema(
                mr_url=state.mr_url,
                mr_description=state.mr_description or "",
                linked_issues=state.linked_issues,
                gating=state.gating,
            )

            prompt = render_prompt(input_data)
            prompt += f"\n\nMR_COMMENTS (JSON, pass as-is to parse_mr_test_results): {json.dumps(state.mr_comments)}"

            response = await agent.run(
                prompt,
                expected_output=MrTestingAnalystResult,
                **get_agent_execution_config(),  # type: ignore
            )

            state.result = MrTestingAnalystResult.model_validate_json(response.last_message.text)
            logger.info(
                "MR testing analysis completed for %s: %s",
                state.mr_url,
                state.result.model_dump_json(indent=4),
            )

            return "act_on_result"

        async def act_on_result(state: MrTestingAnalystWorkflowState):
            """Deterministically post everything the agent decided on in
            run_analysis. dry_run/poc/no_jira_comments are handled entirely
            here -- the agent's own prompt never has to reason about them."""
            result = state.result
            if result is None:
                raise RuntimeError("act_on_result reached with no result set on state")

            if state.dry_run:
                logger.info(
                    "[DRY RUN] Would post %d MR comment(s) and %d Jira comment(s) for %s%s",
                    len(result.mr_comments),
                    0 if state.no_jira_comments else len(result.jira_comments),
                    state.mr_url,
                    f", and resolve discussion {result.resolve_discussion_id}"
                    if result.resolve_discussion_id
                    else "",
                )
                return Workflow.END

            for comment in result.mr_comments:
                if comment.kind == "update":
                    if comment.note_id is None:
                        logger.warning(
                            "mr_comment kind=update has no note_id for %s, posting as a new comment instead",
                            state.mr_url,
                        )
                        await run_tool(
                            "add_merge_request_comment",
                            available_tools=gateway_tools,
                            merge_request_url=state.mr_url,
                            comment=comment.body,
                        )
                    else:
                        await run_tool(
                            "update_merge_request_comment",
                            available_tools=gateway_tools,
                            merge_request_url=state.mr_url,
                            note_id=comment.note_id,
                            comment=comment.body,
                        )
                elif comment.kind == "blocking":
                    tool_name = (
                        "add_merge_request_comment" if state.poc else "add_blocking_merge_request_comment"
                    )
                    await run_tool(
                        tool_name,
                        available_tools=gateway_tools,
                        merge_request_url=state.mr_url,
                        comment=comment.body,
                    )
                else:
                    await run_tool(
                        "add_merge_request_comment",
                        available_tools=gateway_tools,
                        merge_request_url=state.mr_url,
                        comment=comment.body,
                    )

            if not state.no_jira_comments:
                for jira_comment in result.jira_comments:
                    await run_tool(
                        "add_jira_comment",
                        available_tools=gateway_tools,
                        issue_key=jira_comment.jira_key,
                        comment=jira_comment.body,
                        private=True,
                    )

            if result.resolve_discussion_id:
                await run_tool(
                    "resolve_merge_request_discussion",
                    available_tools=gateway_tools,
                    merge_request_url=state.mr_url,
                    discussion_id=result.resolve_discussion_id,
                )

            if result.analyzed and not state.gating:
                await _mark_mr_analyzed(state.mr_url, gateway_tools, state.dry_run)

            logger.info("MR testing analyst workflow completed for %s", state.mr_url)
            return Workflow.END

        workflow.add_step("fetch_and_validate_mr", fetch_and_validate_mr)
        workflow.add_step("fetch_test_results", fetch_test_results)
        workflow.add_step("gather_jira_context", gather_jira_context)
        workflow.add_step("run_analysis", run_analysis)
        workflow.add_step("act_on_result", act_on_result)

        response = await workflow.run(
            MrTestingAnalystWorkflowState(
                mr_url=mr_url,
                dry_run=dry_run,
                poc=poc,
                no_jira_comments=no_jira_comments,
                gating=gating,
            )
        )

        return response.state.result


async def main() -> None:
    configure_logging(level=logging.INFO)

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    poc = os.getenv("POC", "false").lower() == "true"
    no_jira_comments = os.getenv("NO_JIRA_COMMENTS", "false").lower() == "true"
    gating = os.getenv("GATING", "false").lower() == "true"

    mr_url = os.getenv("MR_URL")
    if not mr_url:
        logger.error("MR_URL environment variable is required")
        sys.exit(1)

    logger.info(
        "Running MR testing analysis for %s (dry_run=%s, poc=%s, no_jira_comments=%s, gating=%s)",
        mr_url,
        dry_run,
        poc,
        no_jira_comments,
        gating,
    )
    with span_processor.start_transaction(mr_url, workflow="MrTestingAnalystWorkflow"):
        result = await run_mr_testing_analyst(
            mr_url,
            dry_run=dry_run,
            poc=poc,
            no_jira_comments=no_jira_comments,
            gating=gating,
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
