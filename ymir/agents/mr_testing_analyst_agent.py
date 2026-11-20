import asyncio
import contextlib
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
from ymir.common.base_utils import fix_await, install_shutdown_handler, redis_client, run_task_loop
from ymir.common.constants import RedisQueues
from ymir.common.cve import extract_cve_ids
from ymir.common.logging_setup import configure_logging, get_trajectory_writeable
from ymir.common.models import ErrorData, ErrorListEntry, Task
from ymir.common.utils import init_sentry
from ymir.tools.privileged.mr_testing import fold_comment_body
from ymir.tools.unprivileged.greenwave import FetchTestingFarmResultsTool

logger = logging.getLogger(__file__)
redis_logger = logging.getLogger("agent.redis")

TEST_COVERAGE_CUSTOM_FIELD = "customfield_10638"
MR_TESTING_ANALYZED_LABEL = "mr_testing_analyzed"
MR_TESTING_COMPLETED_LABEL = "mr_testing_completed"

# Matches whole `Resolves: RHEL-1, RHEL-2` lines, in commit messages or an MR
# description; the keys are then picked out of the captured remainder.
RESOLVES_LINE_RE = re.compile(r"^\s*Resolves:(.*)$", re.IGNORECASE | re.MULTILINE)
JIRA_KEY_RE = re.compile(r"RHEL-\d+", re.IGNORECASE)

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
    "format_mr_testing_comment",
    # Gating mode (OSCI bot pipeline results, no anewa/NEWA run)
    "get_pipeline_job_trace",
    "extract_tf_request_from_job_trace",
    # Read-only history lookup for failure analysis (earlier analyses of this component)
    "search_gitlab_project_mrs",
    "get_merge_request_discussions",
]


class MrTestingTaskData(BaseModel):
    """Metadata of a Task on the MR testing queue."""

    mr_url: str = Field(description="URL of the GitLab merge request to analyze")
    gating: bool = Field(default=False, description="Analyze the OSCI gating pipeline instead of anewa/NEWA")


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
            "results discussion thread (via resolve_osci_discussion below) only when a failure is "
            "judged to be bad infrastructure or a broken test, not a real regression."
        ),
    )
    parsed_results_json: str = Field(
        description="JSON-encoded output of the parse tool Python already ran on the MR's comments"
    )


class MrCommentToPost(BaseModel):
    kind: Literal["new", "update", "blocking"] = Field(
        description=(
            "'new': post a plain new comment. "
            "'update': replace an existing comment in place (note_id required) -- used only to "
            "fold the Testing Farm Results summary into anewa's own completed comment. "
            "'blocking': this needs explicit human sign-off (Untested Issues / Test Failure "
            "Analysis) -- posted as an unresolved discussion thread, or a plain comment in poc "
            "mode; which one is Python's decision, not yours."
        )
    )
    body: str = Field(
        description="Fully formatted comment markdown, built via format_mr_testing_comment -- "
        "never hand-composed. For kind='update', just the formatted testing_farm_markdown/"
        "gating_results_markdown output -- Python folds it into anewa's note"
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
    resolve_osci_discussion: bool = Field(
        default=False,
        description="Gating mode only: true if every failed/errored gating job's assessment is "
        "exactly 'Infrastructure issue' or 'Test bug' per Step 5/8 of the prompt.",
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

    parsed_results: dict[str, Any] = Field(default_factory=dict)
    note_id: Any = None
    note_body: str | None = None
    osci_pipeline_id: str | None = None

    result: MrTestingAnalystResult | None = Field(default=None)


def _extract_linked_issues(mr_description: str, commit_messages: list[str] | None = None) -> list[str]:
    """Extract Jira keys from ``Resolves: RHEL-XXXXX`` lines in the commit
    messages and in the MR description. Ymir's own MRs carry these only in
    commit messages (the description's length limit can truncate them), but
    MRs opened by hand may carry them in the description instead."""
    keys: set[str] = set()
    for text in [mr_description or "", *(commit_messages or [])]:
        for remainder in RESOLVES_LINE_RE.findall(text):
            keys.update(key.upper() for key in JIRA_KEY_RE.findall(remainder))
    return sorted(keys)


def _parse_mr_url(mr_url: str) -> tuple[str, int]:
    """Parse a GitLab MR URL into (project_path, iid), for fetch_gitlab_mr_notes'
    project/mr_iid input -- the one gateway tool in this agent that doesn't
    take merge_request_url directly."""
    match = _MR_URL_RE.search(mr_url)
    if not match:
        raise ValueError(f"Could not parse merge request URL: {mr_url}")
    return match.group(1), int(match.group(2))


def _comment_text(comment: dict[str, Any]) -> str:
    """Return a comment's text body, whatever the gateway tool called it --
    mirrors mr_testing.py's own private helper of the same name, duplicated
    here (rather than imported) since it's a one-line local text extraction,
    not something worth coupling this module to that one's internals for."""
    return comment.get("body") or comment.get("message") or ""


def _comments_contain_expected_marker(comments: list[dict[str, Any]], gating: bool) -> bool:
    """Whether `comments` actually contains the results-bot marker this mode
    expects to find. Used to detect get_authorized_comments_from_merge_request
    silently filtering out the results bot's own note (it only returns notes
    from Developer-role-or-higher MR members) on an otherwise *successful*
    call -- unlike an outright exception, that case wouldn't otherwise trigger
    the fetch_gitlab_mr_notes fallback at all. See fetch_test_results."""
    if gating:
        return any("<!--RESULTS_COMMENT-->" in _comment_text(c) for c in comments)
    return any("**ANEWA:**" in _comment_text(c) or re.search(r"\bNEWA\b", _comment_text(c)) for c in comments)


async def _fetch_mr_notes_fallback(mr_url: str, gateway_tools: list) -> list[dict[str, Any]]:
    """Fetch every note on the MR via fetch_gitlab_mr_notes -- unlike
    get_authorized_comments_from_merge_request, this doesn't filter by
    author role, so it still sees a results bot that isn't a Developer-role-
    or-higher MR member. Unlike every other MR-related gateway tool used in
    this agent, fetch_gitlab_mr_notes takes project/mr_iid rather than
    merge_request_url, and returns its result as a JSON string body
    (StringToolOutput), not structured JSON -- both quirks are handled here."""
    project, mr_iid = _parse_mr_url(mr_url)
    comments_result = await run_tool(
        "fetch_gitlab_mr_notes",
        available_tools=gateway_tools,
        project=project,
        mr_iid=mr_iid,
    )
    if isinstance(comments_result, str):
        comments_result = json.loads(comments_result)

    if isinstance(comments_result, dict):
        return comments_result.get("comments", [])
    if isinstance(comments_result, list):
        return comments_result
    return []


async def _find_osci_discussion_to_resolve(
    mr_url: str, gateway_tools: list, osci_pipeline_id: str | None
) -> str | None:
    """Find OSCI bot's own ``<!--RESULTS_COMMENT-->`` discussion thread to
    resolve -- computed here in Python, never supplied directly by the model
    (see MrTestingAnalystResult.resolve_osci_discussion), so a compromised or
    confused model response can't make this agent resolve an arbitrary
    discussion thread.

    A match must have the results-comment marker, be resolvable, not already
    resolved, and (if ``osci_pipeline_id`` is known) embed that pipeline ID in
    its body -- there can be more than one OSCI results thread in the MR's
    history if the pipeline was re-run. If more than one still matches, the
    most recently created one is used. Returns None (after logging a warning)
    if nothing matches -- this never resolves blind.
    """
    discussions = await run_tool(
        "get_merge_request_discussions",
        available_tools=gateway_tools,
        merge_request_url=mr_url,
    )

    candidates = []
    for discussion in discussions:
        body = discussion.get("body") or ""
        if "<!--RESULTS_COMMENT-->" not in body:
            continue
        if not discussion.get("resolvable") or discussion.get("resolved"):
            continue
        if osci_pipeline_id and osci_pipeline_id not in body:
            continue
        candidates.append(discussion)

    if not candidates:
        logger.warning(
            "resolve_osci_discussion was set for %s but no matching unresolved OSCI results "
            "discussion was found (osci_pipeline_id=%s) -- skipping, never resolving blind.",
            mr_url,
            osci_pipeline_id,
        )
        return None

    candidates.sort(key=lambda d: d.get("created_at") or "")
    return candidates[-1].get("discussion_id")


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
            """Fetch MR details and extract linked Jira issues from its commits and description."""
            logger.info("Fetching merge request details for %s", state.mr_url)
            mr_details = await run_tool(
                "get_merge_request_details",
                available_tools=gateway_tools,
                merge_request_url=state.mr_url,
            )
            state.mr_description = mr_details.get("description") or ""

            try:
                commit_messages = await run_tool(
                    "get_merge_request_commit_messages",
                    available_tools=gateway_tools,
                    merge_request_url=state.mr_url,
                )
            except Exception as e:
                # Proceeding on the description alone could wrongly conclude
                # there's nothing to analyze and label the MR as analyzed on
                # what might just be a transient GitLab failure.
                logger.warning("Failed to fetch commits for %s, aborting run: %s", state.mr_url, e)
                state.result = MrTestingAnalystResult(
                    analyzed=False,
                    summary=f"Failed to fetch commits of {state.mr_url}: {e}. Aborting without "
                    "posting or labeling so this can be retried once GitLab is reachable again.",
                )
                return Workflow.END

            state.linked_issue_keys = _extract_linked_issues(state.mr_description, commit_messages)
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
                    summary=f"No `Resolves: RHEL-XXXXX` line found in the commits or description of {state.mr_url}.",
                )
                return Workflow.END

            logger.info("Linked issues for %s: %s", state.mr_url, state.linked_issue_keys)
            return "fetch_test_results"

        async def fetch_test_results(state: MrTestingAnalystWorkflowState):
            """Fetch raw MR comments. get_authorized_comments_from_merge_request
            only returns notes from Developer-role-or-higher MR members -- if the
            results bot isn't one, its note is silently absent even from a
            *successful* call, not just a failed one. So a successful response is
            also checked for the marker this mode expects before being trusted,
            falling back to fetch_gitlab_mr_notes (which isn't role-filtered) the
            same way an outright exception already does."""
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
                state.mr_comments = await _fetch_mr_notes_fallback(state.mr_url, gateway_tools)
                return "parse_mr_results"

            if isinstance(comments_result, dict):
                comments = comments_result.get("comments", [])
            elif isinstance(comments_result, list):
                comments = comments_result
            else:
                comments = []

            if not _comments_contain_expected_marker(comments, state.gating):
                logger.warning(
                    "get_authorized_comments_from_merge_request returned no results-bot marker "
                    "for %s in %s mode -- falling back to fetch_gitlab_mr_notes in case the bot's "
                    "note was filtered out by author role.",
                    state.mr_url,
                    "gating" if state.gating else "anewa/NEWA",
                )
                fallback_comments = await _fetch_mr_notes_fallback(state.mr_url, gateway_tools)
                if _comments_contain_expected_marker(fallback_comments, state.gating):
                    comments = fallback_comments
                # Else keep the original (possibly genuinely empty) result --
                # no results posted yet is a legitimate state, not an error.

            state.mr_comments = comments

            return "parse_mr_results"

        async def parse_mr_results(state: MrTestingAnalystWorkflowState):
            """Deterministically parse the raw MR comments fetched above, in
            Python, rather than handing the model the raw MR_COMMENTS JSON to
            parse itself via tool calls -- a paraphrase/truncation by the model
            while copying anewa's note_body for the later fold step
            (act_on_result) would otherwise corrupt or permanently lose anewa's
            original comment."""
            if state.gating:
                parsed = await run_tool(
                    "parse_osci_results_comment",
                    available_tools=gateway_tools,
                    comments=state.mr_comments,
                )
                state.parsed_results = parsed
                state.osci_pipeline_id = parsed.get("pipeline_id")
                return "gather_jira_context"

            parsed = await run_tool(
                "parse_mr_test_results",
                available_tools=gateway_tools,
                comments=state.mr_comments,
            )
            if parsed.get("note_id") is not None or parsed.get("results_by_arch"):
                state.parsed_results = parsed
                state.note_id = parsed.get("note_id")
                state.note_body = parsed.get("note_body")
            else:
                # No anewa comment (or anewa hasn't finished yet) -- fall back
                # to NEWA's own, much sparser comment. NEWA's comment is never
                # folded into, so note_id/note_body stay unset.
                state.parsed_results = await run_tool(
                    "parse_newa_mr_comment",
                    available_tools=gateway_tools,
                    comments=state.mr_comments,
                )

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
                with contextlib.suppress(TypeError, AttributeError):
                    test_coverage = [v.get("value") for v in test_coverage_raw if v.get("value")]

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
                parsed_results_json=json.dumps(state.parsed_results),
            )

            prompt = render_prompt(input_data)

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
                    ", and consider resolving the OSCI results discussion"
                    if state.gating and result.resolve_osci_discussion
                    else "",
                )
                return Workflow.END

            # Posting mr_comments is left unguarded -- if it fails partway
            # through, let the exception propagate and skip labeling (below),
            # so the whole run can be safely retried without risking
            # duplicate CVE/blocking comments from a partial success.
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
                        # The model never folds this itself (fold_mr_comment_body
                        # isn't in its toolset) -- it just supplies the plain
                        # formatted markdown, and Python folds it into anewa's
                        # own note_body here, so a model paraphrase/truncation
                        # can never corrupt or lose anewa's original comment.
                        body = (
                            fold_comment_body(state.note_body, comment.body)
                            if state.note_body
                            else comment.body
                        )
                        await run_tool(
                            "update_merge_request_comment",
                            available_tools=gateway_tools,
                            merge_request_url=state.mr_url,
                            note_id=comment.note_id,
                            comment=body,
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

            # Labeling happens right after mr_comments succeeds, and no longer
            # depends on the Jira-comments loop or discussion resolution below
            # -- a failure in either of those must never block the MR from
            # being marked analyzed once its own comments are up.
            if result.analyzed and not state.gating:
                await _mark_mr_analyzed(state.mr_url, gateway_tools, state.dry_run)

            if not state.no_jira_comments:
                try:
                    for jira_comment in result.jira_comments:
                        await run_tool(
                            "add_jira_comment",
                            available_tools=gateway_tools,
                            issue_key=jira_comment.jira_key,
                            comment=jira_comment.body,
                            private=True,
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to post one or more Jira comments for %s, continuing: %s",
                        state.mr_url,
                        e,
                    )

            if state.gating and result.resolve_osci_discussion:
                try:
                    discussion_id = await _find_osci_discussion_to_resolve(
                        state.mr_url, gateway_tools, state.osci_pipeline_id
                    )
                    if discussion_id is not None:
                        await run_tool(
                            "resolve_merge_request_discussion",
                            available_tools=gateway_tools,
                            merge_request_url=state.mr_url,
                            discussion_id=discussion_id,
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to resolve OSCI results discussion for %s, continuing: %s",
                        state.mr_url,
                        e,
                    )

            logger.info("MR testing analyst workflow completed for %s", state.mr_url)
            return Workflow.END

        workflow.add_step("fetch_and_validate_mr", fetch_and_validate_mr)
        workflow.add_step("fetch_test_results", fetch_test_results)
        workflow.add_step("parse_mr_results", parse_mr_results)
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


def _print_result(result: MrTestingAnalystResult) -> None:
    state_icon = "✅" if result.analyzed else "⚠️"

    separator = "=" * 60
    print(f"\n{separator}")
    print(f"  RESULT: {state_icon}  {'ANALYZED' if result.analyzed else 'SKIPPED'}")
    print(separator)
    if result.summary:
        print(result.summary)
    print(f"{separator}\n")


async def main() -> None:
    init_sentry()

    configure_logging(level=logging.INFO)

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    poc = os.getenv("POC", "false").lower() == "true"
    no_jira_comments = os.getenv("NO_JIRA_COMMENTS", "false").lower() == "true"

    # Direct mode: run with environment variables
    if mr_url := os.getenv("MR_URL"):
        gating = os.getenv("GATING", "false").lower() == "true"
        logger.info(
            "Running MR testing analysis for %s in direct mode (dry_run=%s, poc=%s, "
            "no_jira_comments=%s, gating=%s)",
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
            _print_result(result)
        return

    # Queue mode
    logger.info("Starting MR testing analyst agent in queue mode")
    max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", 1))
    max_retries = int(os.getenv("MAX_RETRIES", 3))
    queue = RedisQueues.MR_TESTING_QUEUE.value
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        redis_logger.info(
            f"Connected to Redis, max retries set to {max_retries}, listening to queue: {queue}"
        )

        async def push_error(details: str, task: Task | None = None) -> None:
            error_id = await fix_await(redis.incr(RedisQueues.ERROR_ID_COUNTER.value))
            entry = ErrorListEntry(
                error_id=error_id,
                queue=queue if task is not None else None,
                task=task,
                error=ErrorData(details=details, jira_issue="unknown"),
            )
            await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, entry.model_dump_json()))

        async def process_task(payload):
            try:
                task = Task.model_validate_json(payload)
            except Exception as e:
                logger.error(f"Failed to parse task payload, skipping: {e}")
                await push_error(f"Malformed task payload: {e}")
                return

            try:
                task_data = MrTestingTaskData.model_validate(task.metadata)
            except Exception as e:
                # Task itself parsed fine -- keep it on the error entry so it can
                # be requeued once the metadata/schema issue is fixed.
                logger.error(f"Failed to parse task metadata, skipping: {e}")
                await push_error(f"Malformed task metadata: {e}", task)
                return

            logger.info(
                "Processing MR testing analysis for %s (gating=%s, attempt: %d)",
                task_data.mr_url,
                task_data.gating,
                task.attempts + 1,
            )
            try:
                with span_processor.start_transaction(task_data.mr_url, workflow="MrTestingAnalystWorkflow"):
                    result = await run_mr_testing_analyst(
                        task_data.mr_url,
                        dry_run=dry_run,
                        poc=poc,
                        no_jira_comments=no_jira_comments,
                        gating=task_data.gating,
                    )
            except Exception as e:
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {task_data.mr_url}: {e}"
                    )
                    await fix_await(redis.lpush(queue, task.model_dump_json()))
                    return
                logger.error(
                    f"Task failed after {max_retries} attempts, moving to error list: {task_data.mr_url}"
                )
                await push_error(f"MR testing analysis of {task_data.mr_url} failed: {e}", task)
                return

            # analyzed=False is a clean outcome (testing not finished yet, nothing to
            # analyze, or a transient fetch failure that left the MR unlabeled), not a
            # task failure -- whatever enqueues tasks decides when to queue the MR again.
            _print_result(result)

        shutdown_event = asyncio.Event()
        install_shutdown_handler(asyncio.get_running_loop(), shutdown_event)
        await run_task_loop(
            redis,
            [queue],
            process_task,
            max_concurrent=max_concurrent_tasks,
            shutdown_event=shutdown_event,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
