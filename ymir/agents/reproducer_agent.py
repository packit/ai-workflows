import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import sentry_sdk
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field

import ymir.agents.tasks as tasks
from ymir.agents.constants import I_AM_YMIR, mr_description_footer
from ymir.agents.observability import setup_observability
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.tf_cleanup_middleware import TFReservationCleanupMiddleware
from ymir.agents.utils import (
    build_agent_factory_with_mock_repos,
    check_subprocess,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    init_sentry,
    is_reasoning_enabled,
    mcp_tools,
    render_template,
    resolve_chat_model_override,
    run_subprocess,
    run_tool,
)
from ymir.common.base_utils import fix_await, redis_client, run_task_loop
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.delayed_queue import promote_due_tasks, schedule_task
from ymir.common.logging_setup import configure_logging, current_jira_issue
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    ErrorData,
    MergeRequestDetails,
    Task,
)
from ymir.common.models import (
    ReproducerInputSchema as InputSchema,
)
from ymir.common.models import (
    ReproducerOutputSchema as OutputSchema,
)
from ymir.common.reproducer_lock import (
    enqueue_blocked_reproducer_task,
    release_reproducer_lock,
    resolve_reproducer_lock_id,
    sweep_stale_reproducer_locks,
    try_acquire_reproducer_lock,
)
from ymir.tools.privileged.jira import fetch_jira_issue_issuelinks
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.text import CreateTool, SearchTextTool, ViewTool
from ymir.tools.unprivileged.version_mapper import VersionMapperTool

logger = logging.getLogger(__file__)
redis_logger = logging.getLogger("agent.redis")

_REPRODUCER_TERMINAL_LABELS = {
    JiraLabels.REPRODUCER_CREATED.value,
    JiraLabels.REPRODUCER_FAILED.value,
    JiraLabels.REPRODUCER_ERRORED.value,
    JiraLabels.REPRODUCER_NOT_REPRODUCIBLE.value,
    JiraLabels.REPRODUCER_ALREADY_EXISTS.value,
}

_PROMPT_TEMPLATE = "reproducer/prompt.j2"


# MCP tool names the reproducer agent needs access to
_REPRODUCER_MCP_TOOLS = [
    "get_jira_details",
    "get_patch_from_url",
    "get_maintainer_rules",
    "clone_repository",
    "list_project_merge_requests",
    "list_testing_farm_composes",
    "reserve_testing_farm_machine",
    "get_testing_farm_reservation_details",
    "cancel_testing_farm_request",
    "run_remote_command",
    "copy_files_to_remote",
]


class ReproducerState(BaseModel):
    jira_issue: str
    result: OutputSchema | None = Field(default=None)


def create_reproducer_agent(gateway_tools, local_tool_options=None, extra_middlewares=None) -> ReasoningAgent:
    middlewares = [GlobalTrajectoryMiddleware(pretty=True)]
    if extra_middlewares:
        middlewares.extend(extra_middlewares)
    llm = get_chat_model()
    # manage_context must piggyback on the same turn as other tools
    llm.allow_parallel_tool_calls = True
    return ReasoningAgent(
        name="ReproducerAgent",
        llm=llm,
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        enable_context_management=True,
        tools=[
            ThinkTool(),
            RunShellCommandTool(options=local_tool_options) if local_tool_options else RunShellCommandTool(),
            VersionMapperTool(),
            CreateTool(options=local_tool_options) if local_tool_options else CreateTool(),
            ViewTool(options=local_tool_options) if local_tool_options else ViewTool(),
            SearchTextTool(options=local_tool_options) if local_tool_options else SearchTextTool(),
        ]
        + [t for t in gateway_tools if t.name in _REPRODUCER_MCP_TOOLS],
        memory=UnconstrainedMemory(),
        middlewares=middlewares,
        role="Red Hat Enterprise Linux developer",
        instructions=[
            "Do not perform root cause analysis or source code tracing — use the provided triage summary.",
            "Always return the Testing Farm machine by calling cancel_testing_farm_request "
            "when done, even if the reproducer failed.",
            "When constructing patch URLs for upstream commits, always use https://. "
            "If https:// fails when validating the patch with get_patch_from_url, "
            "retry with http:// instead.",
            "Never use shallow clones (--depth) when cloning upstream repositories.",
            "When conversation history contains failed approaches, large obsolete dumps, or "
            "noise you no longer need, call manage_context in the SAME turn as your next useful "
            "tool. Put all still-needed facts in durable_summary (paths, CVE/issue IDs, TF "
            "request id, what already failed and must not be retried, current hypothesis). "
            "Never call manage_context alone. Never restate the task or system instructions in "
            "the summary — those remain available outside compacted history.",
        ],
        notes=[
            "You may call manage_context together with another useful tool in the same turn; "
            "that is the required way to compact context without an extra inference round.",
        ],
    )


class _PromptContext(InputSchema):
    """Combined context for prompt template rendering.

    Extends the input schema with ``dry_run`` so the template can branch
    on it. Defined at module level to avoid re-creating the class on every
    ``_render_prompt`` call.
    """

    dry_run: bool = Field(default=False)
    reproducer_working_dir: str = Field(description="Per-issue working directory on the shared git volume")


def _render_prompt(input_data: InputSchema, dry_run: bool = False) -> str:
    """Render the reproducer prompt template with the input schema fields."""
    working_dir = (
        Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / "Reproducer" / input_data.jira_issue
    )
    context = _PromptContext(
        **input_data.model_dump(),
        dry_run=dry_run,
        reproducer_working_dir=str(working_dir),
    )
    return render_template(_PROMPT_TEMPLATE, context)


def _determine_result_label(result: OutputSchema) -> JiraLabels:
    """Map reproducer output to the appropriate Jira label."""
    if result.adapted_existing and result.success:
        return JiraLabels.REPRODUCER_CREATED
    if result.test_already_exists:
        return JiraLabels.REPRODUCER_ALREADY_EXISTS
    if result.success:
        return JiraLabels.REPRODUCER_CREATED
    if result.not_reproducible_reason:
        return JiraLabels.REPRODUCER_NOT_REPRODUCIBLE
    return JiraLabels.REPRODUCER_FAILED


def _determine_comment_resolution(result: OutputSchema) -> str:
    """Human-readable resolution string for the Jira comment."""
    if result.adapted_existing and result.success:
        return "adapted-existing"
    label = _determine_result_label(result)
    return {
        JiraLabels.REPRODUCER_CREATED: "reproduced",
        JiraLabels.REPRODUCER_NOT_REPRODUCIBLE: "not-reproducible",
        JiraLabels.REPRODUCER_ALREADY_EXISTS: "already-exists",
        JiraLabels.REPRODUCER_FAILED: "failed",
    }.get(label, "failed")


def _should_finalize_jira(result: OutputSchema) -> bool:
    """Whether handle_results should write terminal labels/comments.

    Retryable infra errors and lock contention keep
    ``ymir_reproducer_in_progress`` and are scheduled for a later attempt.
    """
    return not result.retryable_error and not result.lock_deferred


def _needs_merge_request(result: OutputSchema) -> bool:
    """Whether orchestration should commit/push an MR for this result."""
    if result.lock_deferred or result.retryable_error:
        return False
    if result.adapted_existing and result.success:
        return True
    if result.test_already_exists and not result.adapted_existing:
        return False
    return result.success


def _resolve_test_dir(tests_clone: Path, test_directory: str | None) -> Path | None:
    """Resolve the agent-reported relative test path under the tests clone.

    The agent owns naming (``Security/<CVE>``, ``Regression/<JIRA>``, or any
    other relative path it created). Orchestration never invents the location.
    Absolute paths and ``..`` segments are rejected.
    """
    if not test_directory or not test_directory.strip():
        return None

    relative = test_directory.strip().lstrip("/")
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        logger.warning("Rejecting unsafe test_directory from agent: %r", test_directory)
        return None

    resolved = (tests_clone / relative).resolve()
    try:
        resolved.relative_to(tests_clone.resolve())
    except ValueError:
        logger.warning(
            "test_directory %r resolves outside tests clone %s",
            test_directory,
            tests_clone,
        )
        return None

    if resolved.is_dir() and any(resolved.iterdir()):
        return resolved
    return None


def _cve_only_needles(cve_id: str | None) -> list[str]:
    """CVE id strings used to match sibling-stream reproducer MRs."""
    if not cve_id or not cve_id.strip():
        return []
    return sorted({p.strip().upper() for p in cve_id.replace(";", ",").split(",") if p.strip()})


_REPRODUCER_MR_BRACKET_CVE = re.compile(r"\[(CVE-\d{4}-\d+)\]", re.IGNORECASE)
_REPRODUCER_MR_BRACKET_JIRA = re.compile(r"\[(RHEL-\d+)\]", re.IGNORECASE)


def _reproducer_mr_title_tags(title: str) -> tuple[set[str], set[str]]:
    """Parse canonical ``[CVE-…]`` / ``[RHEL-…]`` tags from an MR title."""
    cves = {match.upper() for match in _REPRODUCER_MR_BRACKET_CVE.findall(title)}
    jiras = {match.upper() for match in _REPRODUCER_MR_BRACKET_JIRA.findall(title)}
    return cves, jiras


def _build_mr_title(
    result: OutputSchema,
    input_data: InputSchema,
    *,
    matched_mr: dict | None = None,
) -> str:
    """Build MR title keyed by bracket tags in the title (title-only matching).

    CVE reproducers use a single ``[CVE-…]`` tag (stable across streams).
    Regression reproducers accumulate ``[RHEL-…]`` keys when another stream's
    job updates the same open MR.
    """
    cves = _cve_only_needles(input_data.cve_id)
    if result.reproducer_type == "cve" and cves:
        tags = " ".join(f"[{cve}]" for cve in cves)
        return f"{result.package}: {tags} ymir reproducer test"

    jiras = {result.jira_issue.upper()}
    if matched_mr:
        _, existing_jiras = _reproducer_mr_title_tags(matched_mr.get("title") or "")
        jiras |= existing_jiras
    tag = "[" + ", ".join(sorted(jiras)) + "]"
    return f"{result.package}: {tag} ymir reproducer test"


def _is_reproducer_mr_title(title: str) -> bool:
    return "ymir reproducer test" in title.lower()


def _match_regression_sibling_mr(mrs: list[dict], jira_issue: str) -> dict | None:
    """Find the canonical regression reproducer MR to extend for another stream.

    When the current issue is not yet listed in the title, match a sole open
    regression reproducer MR (no ``[CVE-…]`` tag in the title).
    """
    wanted = jira_issue.upper()
    candidates: list[dict] = []
    for mr in mrs:
        title = mr.get("title") or ""
        title_cves, title_jiras = _reproducer_mr_title_tags(title)
        if title_cves or not title_jiras or not _is_reproducer_mr_title(title):
            continue
        if wanted in title_jiras:
            return mr
        candidates.append(mr)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _match_open_reproducer_mr(
    mrs: list[dict],
    *,
    cve_ids: list[str] | None = None,
    jira_issue: str | None = None,
    existing_mr_url: str | None = None,
) -> dict | None:
    """Return the open reproducer MR for this CVE or Jira issue.

    Matching uses **MR title only** via ``[CVE-…]`` or ``[RHEL-…]`` bracket
    tags (see ``_build_mr_title``). Descriptions are ignored.
    """
    if existing_mr_url:
        for mr in mrs:
            if mr.get("url") == existing_mr_url:
                return mr

    wanted_cves = {cve.upper() for cve in cve_ids or [] if cve}
    wanted_jira = jira_issue.upper() if jira_issue else None

    for mr in mrs:
        title = mr.get("title") or ""
        title_cves, title_jiras = _reproducer_mr_title_tags(title)

        if wanted_cves and wanted_cves & title_cves:
            return mr
        if wanted_jira and wanted_jira in title_jiras:
            return mr

    return None


async def _list_open_reproducer_mrs(package: str, available_tools: list[Any]) -> list[dict]:
    try:
        listed = await run_tool(
            "list_project_merge_requests",
            project=f"redhat/rhel/tests/{package}",
            state="opened",
            labels=["ymir_reproducer"],
            available_tools=available_tools,
        )
    except Exception as e:
        logger.warning("Failed to list open reproducer MRs for %s: %s", package, e)
        return []

    mrs = json.loads(listed) if isinstance(listed, str) else listed
    return mrs if isinstance(mrs, list) else []


async def _resolve_reproducer_mr_target(
    result: OutputSchema,
    agent_input: InputSchema,
    package: str,
    available_tools: list[Any],
) -> tuple[str | None, str, dict | None]:
    """Resolve MR URL, git branch, and matched MR metadata for create/adapt push.

    When an open ``ymir_reproducer`` MR already exists for the same CVE, sibling
    stream jobs must update that MR's source branch — the MR does not need to be
    merged first. Regression (non-CVE) jobs accumulate ``[RHEL-…]`` keys in the
    MR title when another stream extends the same open MR.
    """
    fallback_branch = f"reproducer/{result.jira_issue}"
    mrs = await _list_open_reproducer_mrs(package, available_tools)

    cve_needles = _cve_only_needles(agent_input.cve_id)
    if cve_needles:
        matched = _match_open_reproducer_mr(
            mrs,
            cve_ids=cve_needles,
            existing_mr_url=result.existing_mr_url,
        )
    else:
        matched = _match_open_reproducer_mr(
            mrs,
            jira_issue=result.jira_issue,
            existing_mr_url=result.existing_mr_url,
        )
        if matched is None and result.reproducer_type == "bug":
            matched = _match_regression_sibling_mr(mrs, result.jira_issue)

    if matched:
        mr_url = matched.get("url")
        branch = matched.get("source_branch") or fallback_branch
        if mr_url:
            result.existing_mr_url = mr_url
        result.adapted_existing = True
        logger.info(
            "Updating existing reproducer MR %s on branch %s for %s",
            mr_url,
            branch,
            result.jira_issue,
        )
        return mr_url, branch, matched

    return result.existing_mr_url, fallback_branch, None


async def _prepare_reproducer_branch(
    tests_clone: Path,
    test_dir: Path,
    update_branch: str,
    *,
    adapted_existing: bool,
    existing_mr_url: str | None,
    available_tools: list[Any],
) -> str:
    """Checkout the commit/push branch while preserving local ``test_dir`` edits.

    For adaptations of an open MR, fetch and check out that MR's source-branch
    tip first (same idea as ``prepare_dist_git_from_merge_request``) so a later
    force-push cannot drop sibling commits already on the MR. New MRs create
    ``update_branch`` from the current local HEAD.
    """
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "adapted_test"
        shutil.copytree(test_dir, snapshot)

        branch = update_branch
        try:
            if existing_mr_url:
                try:
                    details_raw = await run_tool(
                        "get_merge_request_details",
                        merge_request_url=existing_mr_url,
                        available_tools=available_tools,
                    )
                    details = MergeRequestDetails.model_validate(details_raw)
                    branch = details.source_branch or update_branch

                    # Leave the target branch so fetch can update refs/heads/<branch>.
                    _, head_branch, _ = await run_subprocess(
                        ["git", "branch", "--show-current"], cwd=tests_clone
                    )
                    if head_branch.strip() == branch:
                        await check_subprocess(
                            ["git", "checkout", "--detach"],
                            cwd=tests_clone,
                        )

                    await run_tool(
                        "fetch_branch",
                        repository=details.source_repo,
                        branch=branch,
                        clone_path=str(tests_clone),
                        available_tools=available_tools,
                    )
                    await check_subprocess(
                        ["git", "checkout", "-f", branch],
                        cwd=tests_clone,
                    )
                    logger.info(
                        "Checked out existing MR source branch %s for adapt (%s)",
                        branch,
                        existing_mr_url,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to fetch/checkout existing MR branch for %s "
                        "(wanted %s); falling back to checkout -B from local HEAD: %s",
                        existing_mr_url,
                        update_branch,
                        e,
                    )
                    branch = update_branch
                    await check_subprocess(
                        ["git", "checkout", "-B", branch],
                        cwd=tests_clone,
                    )
            else:
                await check_subprocess(
                    ["git", "checkout", "-B", branch],
                    cwd=tests_clone,
                )
        finally:
            # Overlay agent adaptations onto whatever tip we checked out.
            if test_dir.exists():
                shutil.rmtree(test_dir)
            shutil.copytree(snapshot, test_dir)

    return branch


def _build_mr_description(result: OutputSchema, input_data: InputSchema) -> str:
    """Assemble the MR description from the reproducer output."""
    if result.reproducer_type == "cve":
        summary_line = f"Security test for {input_data.cve_id} in {result.package}."
    else:
        summary_line = f"Regression test for {result.jira_issue} in {result.package}."

    verification = f"Verified on Testing Farm (request ID: {result.testing_farm_request_id})."
    if result.compose and result.arch:
        verification += f"\nThe reproducer successfully detected the bug on {result.compose} ({result.arch})."

    return (
        f"## Summary\n\n"
        f"{summary_line}\n\n"
        f"{result.summary}\n\n"
        f"## Pass/Fail Criteria\n\n"
        f"{result.pass_fail_criteria}\n\n"
        f"## Verification\n\n"
        f"{verification}\n\n"
        f"## Test Structure\n\n"
        f"- `ai-test-description` — issue analysis and test specification\n"
        f"- `runtest.sh` — BeakerLib test harness\n"
        f"- `main.fmf` — FMF metadata\n"
        f"- `test_*` — standalone reproducer script(s)\n\n"
        f"Resolves: {result.jira_issue}\n\n"
        f"{mr_description_footer(result.package)}"
    )


def _build_commit_message(result: OutputSchema, input_data: InputSchema) -> str:
    """Build the commit message for the reproducer test."""
    if result.reproducer_type == "cve":
        title = f"{result.package}: add security reproducer for {result.jira_issue}"
        body = f"Add security test for {input_data.cve_id} in {result.package}."
    else:
        title = f"{result.package}: add regression reproducer for {result.jira_issue}"
        body = f"Add regression test for {result.jira_issue} in {result.package}."

    return (
        f"{title}\n\n"
        f"{body}\n\n"
        f"Resolves: {result.jira_issue}\n\n"
        f"This test was created {I_AM_YMIR}\n\n"
        f"Assisted-by: Ymir\n"
    )


async def run_workflow(
    jira_issue: str,
    dry_run: bool,
    reproducer_agent_factory,
    input_data: InputSchema | None = None,
    user_triggered: bool = False,
    redis_conn=None,
):
    local_tool_options = None
    if mock_env := get_mock_local_tool_env(jira_issue):
        local_tool_options = {"env": mock_env}

    call_meta: dict[str, str] = {"jira_issue": jira_issue}
    if input_data and input_data.package:
        call_meta["package"] = input_data.package

    if not jira_issue or Path(jira_issue).is_absolute() or ".." in jira_issue:
        raise ValueError(f"Invalid jira_issue: {jira_issue}")
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / "Reproducer" / jira_issue
    if working_dir.is_dir():
        tasks._force_rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)

    async with mcp_tools(os.getenv("MCP_GATEWAY_URL"), call_meta=call_meta) as gateway_tools:
        tf_cleanup = TFReservationCleanupMiddleware()
        reproducer_agent = reproducer_agent_factory(
            gateway_tools, local_tool_options, extra_middlewares=[tf_cleanup]
        )

        workflow = Workflow(ReproducerState, name="ReproducerWorkflow")

        async def run_reproducer_analysis(state):
            """Run the reproducer agent."""
            logger.info(f"Running reproducer analysis for {state.jira_issue}")

            agent_input = InputSchema(jira_issue=state.jira_issue) if input_data is None else input_data

            response = await reproducer_agent.run(
                _render_prompt(agent_input, dry_run=dry_run),
                expected_output=render_template("reproducer/output_format.j2"),
                **get_agent_execution_config(),
            )
            state.result = OutputSchema.model_validate_json(response.last_message.text)

            # Normalize jira_issue to upper-case
            state.result.jira_issue = state.result.jira_issue.upper()

            return "create_merge_request"

        async def create_merge_request(state):
            """Fork, push, and open or update a merge request for verified reproducers."""
            result = state.result

            if not _needs_merge_request(result):
                logger.info(
                    "Skipping MR creation for %s "
                    "(success=%s, test_already_exists=%s, adapted=%s, lock_deferred=%s)",
                    state.jira_issue,
                    result.success,
                    result.test_already_exists,
                    result.adapted_existing,
                    result.lock_deferred,
                )
                return "handle_results"

            if dry_run:
                logger.info(f"Dry run — skipping MR creation for {state.jira_issue}")
                return "handle_results"

            package = result.package
            agent_input = InputSchema(jira_issue=state.jira_issue) if input_data is None else input_data

            try:
                tests_clone = (
                    Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos"))
                    / "Reproducer"
                    / state.jira_issue
                    / f"tests-{package}"
                )

                if not tests_clone.is_dir():
                    logger.warning(f"Tests clone not found at {tests_clone}, skipping MR creation")
                    result.success = False
                    result.summary += " (MR creation skipped: tests clone directory not found)"
                    return "handle_results"

                test_dir = _resolve_test_dir(tests_clone, result.test_directory)
                if test_dir is None:
                    logger.warning(
                        "Test dir not found for %s (test_directory=%r), skipping MR creation",
                        state.jira_issue,
                        result.test_directory,
                    )
                    result.success = False
                    result.summary += " (MR creation skipped: test directory not found)"
                    return "handle_results"
                logger.info("Using test directory %s for MR creation", test_dir)

                existing_mr_url, update_branch, matched_mr = await _resolve_reproducer_mr_target(
                    result,
                    agent_input,
                    package,
                    gateway_tools,
                )
                update_branch = await _prepare_reproducer_branch(
                    tests_clone,
                    test_dir,
                    update_branch,
                    adapted_existing=bool(result.adapted_existing),
                    existing_mr_url=existing_mr_url,
                    available_tools=gateway_tools,
                )

                # Make shell scripts executable before staging
                for script in test_dir.glob("*.sh"):
                    script.chmod(0o755)
                for script in test_dir.glob("*.ksh"):
                    script.chmod(0o755)

                await check_subprocess(
                    ["git", "add", str(test_dir.relative_to(tests_clone))],
                    cwd=tests_clone,
                )

                # Determine target branch from the clone's default remote HEAD
                target_ref, _ = await check_subprocess(
                    ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
                    cwd=tests_clone,
                )
                target_branch = target_ref.strip().removeprefix("origin/") if target_ref else "main"

                repository = f"https://gitlab.com/redhat/rhel/tests/{package}"
                fork_url = await run_tool(
                    "fork_repository", repository=repository, available_tools=gateway_tools
                )

                mr_title = _build_mr_title(result, agent_input, matched_mr=matched_mr)
                mr_description = _build_mr_description(result, agent_input)
                commit_message = _build_commit_message(result, agent_input)

                mr_url, _ = await tasks.commit_push_and_open_mr(
                    local_clone=tests_clone,
                    commit_message=commit_message,
                    fork_url=fork_url,
                    dist_git_branch=target_branch,
                    update_branch=update_branch,
                    mr_title=mr_title,
                    mr_description=mr_description,
                    available_tools=gateway_tools,
                    labels=["ymir_reproducer"],
                )
                result.test_mr_url = mr_url
                if mr_url:
                    logger.info(f"Created/updated reproducer MR: {mr_url}")
                    if result.adapted_existing:
                        result.existing_mr_url = result.existing_mr_url or mr_url
                else:
                    logger.warning(f"MR creation returned no URL for {state.jira_issue}")
                    result.success = False
                    result.summary += " (MR creation did not return a URL)"

            except Exception as e:
                logger.warning(f"Error creating reproducer MR for {state.jira_issue}: {e}")
                result.test_mr_url = None
                result.success = False
                result.summary += f" (MR creation failed: {e})"

            return "handle_results"

        async def handle_results(state):
            """Set Jira labels and post a comment based on the result."""
            result = state.result
            logger.info(
                f"Reproducer result for {state.jira_issue}: "
                f"success={result.success}, type={result.reproducer_type}, "
                f"retryable_error={result.retryable_error}, "
                f"lock_deferred={result.lock_deferred}"
            )

            if dry_run:
                logger.info(f"Dry run — skipping Jira updates for {state.jira_issue}")
                return Workflow.END

            if not _should_finalize_jira(result):
                logger.info(
                    f"Deferring Jira finalization for {state.jira_issue} — "
                    "leaving ymir_reproducer_in_progress for scheduled retry"
                )
                return Workflow.END

            # Build a human-readable comment
            comment_parts = [
                f"*Resolution*: {_determine_comment_resolution(result)}",
                f"*Reproducer Type*: {result.reproducer_type}",
            ]

            if result.testing_farm_request_id:
                comment_parts.append(f"*Testing Farm Request*: {result.testing_farm_request_id}")

            if result.test_mr_url:
                comment_parts.append(f"*Test MR*: {result.test_mr_url}")
            elif result.existing_mr_url:
                comment_parts.append(f"*Existing Test MR*: {result.existing_mr_url}")

            comment_parts.append(f"\n*Pass/Fail Criteria*:\n{result.pass_fail_criteria}")
            comment_parts.append(f"\n*Summary*:\n{result.summary}")

            if result.not_reproducible_reason:
                comment_parts.append(f"\n*Not Reproducible Reason*:\n{result.not_reproducible_reason}")

            comment_text = "\n".join(comment_parts)

            result_label = _determine_result_label(result)
            await tasks.set_jira_labels(
                jira_issue=state.jira_issue,
                labels_to_add=[result_label.value],
                labels_to_remove=[JiraLabels.REPRODUCER_IN_PROGRESS.value],
                dry_run=dry_run,
                user_triggered=user_triggered,
            )

            await tasks.comment_in_jira(
                jira_issue=state.jira_issue,
                agent_type="Reproducer",
                comment_text=comment_text,
                available_tools=gateway_tools,
                user_triggered=user_triggered,
            )
            return Workflow.END

        workflow.add_step("run_reproducer_analysis", run_reproducer_analysis)
        workflow.add_step("create_merge_request", create_merge_request)
        workflow.add_step("handle_results", handle_results)

        try:
            response = await workflow.run(ReproducerState(jira_issue=jira_issue))
            return response.state
        finally:
            await tf_cleanup.cleanup(gateway_tools)


async def _stage_reproducer_in_progress(
    *,
    jira_issue: str,
    dry_run: bool,
    user_triggered: bool,
    task: Task,
) -> None:
    """Stamp ``ymir_reproducer_in_progress`` before queue work or while blocked on lock."""
    await tasks.set_jira_labels(
        jira_issue=jira_issue,
        labels_to_add=[JiraLabels.REPRODUCER_IN_PROGRESS.value],
        labels_to_remove=list(_REPRODUCER_TERMINAL_LABELS),
        dry_run=dry_run,
        user_triggered=user_triggered,
        critical=True,
    )
    await tasks.post_user_ack_once(
        task=task,
        jira_issue=jira_issue,
        agent_type="Reproducer",
        comment_text=(
            "Ymir picked up your request and started processing. "
            "Results will be posted here when reproducer analysis completes."
        ),
        user_triggered=user_triggered,
        dry_run=dry_run,
    )


async def main() -> None:
    init_sentry()

    configure_logging(level=logging.INFO, buffer_size=int(os.getenv("LOG_BUFFER_SIZE", 0)))
    resolve_chat_model_override("reproducer")

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    if jira_issue := os.getenv("JIRA_ISSUE", None):
        logger.info("Running in direct mode with environment variable")
        with span_processor.start_transaction(jira_issue, workflow="reproducer"):
            agent_factory = build_agent_factory_with_mock_repos(create_reproducer_agent, jira_issue)
            state = await run_workflow(
                jira_issue,
                dry_run,
                agent_factory,
            )
            logger.info(f"Direct run completed: {state.result.model_dump_json(indent=4)}")
            return

    logger.info("Starting reproducer agent in queue mode")
    max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", 1))
    retry_delay_seconds = float(os.getenv("REPRODUCER_RETRY_DELAY_SECONDS", "1800"))
    poll_timeout = int(os.getenv("REPRODUCER_POLL_TIMEOUT", "30"))
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        redis_logger.info(
            "Connected to Redis, max retries set to %s, retry delay %.0fs",
            max_retries,
            retry_delay_seconds,
        )

        def _target_queue_for_delayed_payload(payload: str) -> str:
            try:
                delayed_task = Task.model_validate_json(payload)
            except Exception:
                return RedisQueues.REPRODUCER_QUEUE.value
            return (
                RedisQueues.REPRODUCER_QUEUE_TODO.value
                if delayed_task.user_triggered
                else RedisQueues.REPRODUCER_QUEUE.value
            )

        async def poll_reproducer():
            await sweep_stale_reproducer_locks(redis)
            await promote_due_tasks(
                redis,
                RedisQueues.REPRODUCER_DELAYED_QUEUE.value,
                _target_queue_for_delayed_payload,
            )
            return await fix_await(
                redis.brpop(
                    [RedisQueues.REPRODUCER_QUEUE_TODO.value, RedisQueues.REPRODUCER_QUEUE.value],
                    timeout=poll_timeout,
                )
            )

        async def process_task(payload):
            task = Task.model_validate_json(payload)
            input_data = InputSchema.model_validate(task.metadata)
            current_jira_issue.set(input_data.jira_issue)
            user_triggered = task.user_triggered
            logger.info(
                f"Processing reproducer for JIRA issue: {input_data.jira_issue}, "
                f"attempt: {task.attempts + 1}" + (" (user-triggered)" if user_triggered else "")
            )
            if user_triggered and task.attempts == 0:
                sentry_sdk.metrics.count(
                    "ymir_todo.processed",
                    1,
                    attributes={"issue": input_data.jira_issue},
                )

            # Duplicate-processing guard: skip if the issue already has a
            # reproducer-terminal label and is not currently in-progress or
            # user-triggered (which always gets a fresh run).
            current_labels, _ = await tasks.get_jira_issue_metadata(input_data.jira_issue)
            terminal_ymir_labels = [label for label in current_labels if label in _REPRODUCER_TERMINAL_LABELS]
            if (
                terminal_ymir_labels
                and JiraLabels.REPRODUCER_IN_PROGRESS.value not in current_labels
                and not user_triggered
            ):
                logger.info(
                    f"Skipping duplicate reproducer for {input_data.jira_issue} — "
                    f"already has labels: {terminal_ymir_labels}"
                )
                return

            async def retry(
                task,
                error,
                input_data=input_data,
                user_triggered=user_triggered,
                delay_seconds: float | None = None,
            ):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {input_data.jira_issue}"
                        + (f" (delay={delay_seconds:.0f}s)" if delay_seconds is not None else "")
                    )
                    payload_json = task.model_dump_json()
                    if delay_seconds is not None:
                        await schedule_task(
                            redis,
                            RedisQueues.REPRODUCER_DELAYED_QUEUE.value,
                            payload_json,
                            delay_seconds,
                        )
                    else:
                        retry_queue = (
                            RedisQueues.REPRODUCER_QUEUE_TODO.value
                            if task.user_triggered
                            else RedisQueues.REPRODUCER_QUEUE.value
                        )
                        await fix_await(redis.lpush(retry_queue, payload_json))
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, "
                        f"moving to error list: {input_data.jira_issue}"
                    )
                    try:
                        await tasks.set_jira_labels(
                            jira_issue=input_data.jira_issue,
                            labels_to_add=[JiraLabels.REPRODUCER_ERRORED.value],
                            labels_to_remove=[JiraLabels.REPRODUCER_IN_PROGRESS.value],
                            dry_run=dry_run,
                            user_triggered=user_triggered,
                        )
                    except Exception as label_error:
                        logger.warning(
                            f"Failed to set error labels on {input_data.jira_issue}: {label_error}"
                        )
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            if not input_data.package:
                logger.error(
                    "Reproducer task for %s is missing package metadata; cannot acquire lock",
                    input_data.jira_issue,
                )
                await retry(
                    task,
                    ErrorData(
                        details="Missing package in reproducer task metadata",
                        jira_issue=input_data.jira_issue,
                    ).model_dump_json(),
                )
                return

            lock_id = await resolve_reproducer_lock_id(
                input_data.cve_id,
                input_data.jira_issue,
                fetch_issuelinks=fetch_jira_issue_issuelinks,
            )
            lock_token = await try_acquire_reproducer_lock(
                redis,
                input_data.package,
                lock_id,
                jira_issue=input_data.jira_issue,
            )
            if lock_token is None:
                try:
                    await _stage_reproducer_in_progress(
                        jira_issue=input_data.jira_issue,
                        dry_run=dry_run,
                        user_triggered=user_triggered,
                        task=task,
                    )
                except Exception as e:
                    logger.error(
                        "Could not set %s on blocked reproducer %s: %s",
                        JiraLabels.REPRODUCER_IN_PROGRESS.value,
                        input_data.jira_issue,
                        e,
                    )
                    await retry(
                        task,
                        ErrorData(
                            details=f"Failed to set in-progress label while blocked: {e}",
                            jira_issue=input_data.jira_issue,
                        ).model_dump_json(),
                    )
                    await asyncio.sleep(60)
                    return

                await enqueue_blocked_reproducer_task(
                    redis,
                    input_data.package,
                    lock_id,
                    task.model_dump_json(),
                )
                logger.info(
                    "Reproducer lock busy for %s/%s — blocked %s until lock is released",
                    input_data.package,
                    lock_id,
                    input_data.jira_issue,
                )
                return

            try:
                try:
                    await _stage_reproducer_in_progress(
                        jira_issue=input_data.jira_issue,
                        dry_run=dry_run,
                        user_triggered=user_triggered,
                        task=task,
                    )
                    logger.info(f"Cleaned up existing labels for {input_data.jira_issue}")
                except Exception as e:
                    logger.error(
                        f"Could not set {JiraLabels.REPRODUCER_IN_PROGRESS.value} on "
                        f"{input_data.jira_issue} after retries: {e}; "
                        "re-queuing to avoid duplicate reproducer."
                    )
                    error_msg = f"Failed to set in-progress label: {e}"
                    error_data = ErrorData(details=error_msg, jira_issue=input_data.jira_issue)
                    await retry(task, error_data.model_dump_json())
                    await asyncio.sleep(60)
                    return

                logger.info(f"Starting reproducer processing for {input_data.jira_issue}")
                with span_processor.start_transaction(input_data.jira_issue, workflow="reproducer"):
                    state = await run_workflow(
                        input_data.jira_issue,
                        dry_run,
                        create_reproducer_agent,
                        input_data=input_data,
                        user_triggered=user_triggered,
                        redis_conn=redis,
                    )
                    output = state.result
                    logger.info(
                        f"Reproducer processing completed for {input_data.jira_issue}, "
                        f"success: {output.success}, retryable_error: {output.retryable_error}, "
                        f"lock_deferred: {output.lock_deferred}"
                    )

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during reproducer processing for {input_data.jira_issue}: {error}")
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=input_data.jira_issue).model_dump_json(),
                )
            else:
                if output.retryable_error:
                    logger.info(
                        f"Reproducer retryable infra error for {input_data.jira_issue}; "
                        f"scheduling retry in {retry_delay_seconds:.0f}s"
                    )
                    await retry(
                        task,
                        ErrorData(
                            details=output.summary or "Reproducer deferred: retryable infra error",
                            jira_issue=input_data.jira_issue,
                        ).model_dump_json(),
                        delay_seconds=retry_delay_seconds,
                    )
                else:
                    logger.info(
                        f"Reproducer resolved as success={output.success} for {input_data.jira_issue}"
                    )
                    await fix_await(
                        redis.lpush(
                            RedisQueues.COMPLETED_REPRODUCER_LIST.value,
                            output.model_dump_json(),
                        )
                    )
                    logger.info(
                        f"Pushed {input_data.jira_issue} to {RedisQueues.COMPLETED_REPRODUCER_LIST.value}"
                    )
            finally:
                try:
                    await release_reproducer_lock(
                        redis,
                        input_data.package,
                        lock_id,
                        lock_token,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to release reproducer lock for %s/%s: %s",
                        input_data.package,
                        lock_id,
                        e,
                    )

        await run_task_loop(
            redis,
            [RedisQueues.REPRODUCER_QUEUE_TODO.value, RedisQueues.REPRODUCER_QUEUE.value],
            process_task,
            max_concurrent=max_concurrent_tasks,
            poll_timeout=poll_timeout,
            poll_fn=poll_reproducer,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
