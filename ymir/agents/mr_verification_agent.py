"""MR verification agent — reviews Ymir-authored merge requests.

This agent is a *reader*: it never pushes, never edits the dist-git tree and
never changes a Jira status.  Its only side effects are a review comment plus
a label on the merge request, and (when the review found blockers) a Jira
comment so the issue's watchers see that the MR needs work.

It is fed by ``try_submit_verification_job`` in :mod:`ymir.agents.tasks`,
which every MR-producing agent calls right after opening an MR.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

from beeai_framework.agents.requirement.requirements.conditional import ConditionalRequirement
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field

import ymir.agents.tasks as tasks
from ymir.agents.observability import setup_observability
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    mcp_tools,
    render_template,
    resolve_chat_model_override,
    run_subprocess,
    run_tool,
)
from ymir.common.base_utils import (
    fix_await,
    install_shutdown_handler,
    is_cs_branch,
    redis_client,
    run_task_loop,
)
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.issue_lock import issue_lock
from ymir.common.logging_setup import configure_logging, current_jira_issue, get_trajectory_writeable
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    ErrorData,
    ErrorListEntry,
    MRFindingSeverity,
    MRVerificationInputSchema,
    MRVerificationOutputSchema,
    MRVerificationTaskMetadata,
    MRVerificationVerdict,
    Task,
)
from ymir.common.utils import init_sentry
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.filesystem import GetCWDTool
from ymir.tools.unprivileged.specfile import GetPackageInfoTool
from ymir.tools.unprivileged.text import SearchTextTool, ViewTool
from ymir.tools.unprivileged.wicked_git import GitLogSearchTool, RunPackagePrepTool

logger = logging.getLogger(__name__)
redis_logger = logging.getLogger("agent.redis")

# The MCP gateway writes refs on its own pod; both pods share an NFS4 PVC whose
# attribute cache can serve stale directory listings for up to 30s.  Same
# constant, and the same reason, as in the consolidation agent.
_NFS_CACHE_WAIT = 60

# Per-MR key holding the head SHA that was last reviewed.  Re-reviewing an
# unchanged MR costs a full LLM run and posts a duplicate comment, so skip it.
# One key per MR rather than one big hash so the records expire on their own:
# the repo already has no eviction policy (see data_retention_policy.md) and a
# hash that only ever grows would be one more thing nobody prunes.
_REVIEWED_HEAD_PREFIX = "mr_verification_reviewed_head:"
_REVIEWED_HEAD_TTL = int(os.getenv("MR_VERIFICATION_REVIEWED_TTL", 90 * 24 * 3600))


def _reviewed_head_key(merge_request_url: str) -> str:
    return f"{_REVIEWED_HEAD_PREFIX}{merge_request_url}"


async def already_reviewed(redis_conn, merge_request_url: str, head_sha: str | None) -> bool:
    """Has this exact MR head already been reviewed?"""
    if redis_conn is None or not head_sha:
        return False
    try:
        previous = await fix_await(redis_conn.get(_reviewed_head_key(merge_request_url)))
    except Exception as e:
        # Losing the dedup record costs a duplicate review, not correctness.
        logger.warning("Could not read reviewed head for %s: %s", merge_request_url, e)
        return False
    if isinstance(previous, bytes):
        previous = previous.decode()
    return previous == head_sha


async def record_reviewed_head(
    redis_conn, merge_request_url: str, head_sha: str | None, dry_run: bool = False
) -> None:
    """Remember the reviewed head so an unchanged MR is not reviewed twice.

    A dry run publishes nothing, so recording it would suppress the real review
    that follows.
    """
    if redis_conn is None or not head_sha or dry_run:
        return
    try:
        await fix_await(
            redis_conn.set(_reviewed_head_key(merge_request_url), head_sha, ex=_REVIEWED_HEAD_TTL)
        )
    except Exception as e:
        logger.warning("Failed to record reviewed head for %s: %s", merge_request_url, e)


# Diffs go straight into the prompt; a rebase MR can carry a multi-megabyte
# tarball-ish diff that would blow the context window.  The agent still has
# `view`/`search_text`/`run_shell_command` on the clone to read anything the
# truncated diff cut off.
_MAX_DIFF_CHARS = int(os.getenv("MR_VERIFICATION_MAX_DIFF_CHARS", 120_000))

# Read-only MCP tools the reviewer may use for corroborating evidence.
_ALLOWED_MCP_TOOLS = (
    "get_maintainer_rules",
    "get_shared_rules",
    "get_jira_details",
    "get_patch_from_url",
)

_SEVERITY_ICON = {
    MRFindingSeverity.BLOCKER: "🛑",
    MRFindingSeverity.WARNING: "⚠️",
    MRFindingSeverity.NITPICK: "💡",
}


def create_verification_agent(
    mcp_tools_list: list[Tool],
    local_tool_options: dict[str, Any],
) -> ReasoningAgent:
    """Create the reviewing agent.

    The tool list is deliberately read-only — no ``create``/``str_replace``/
    ``remove``/``git_patch_apply``, and none of the privileged push or Jira
    write tools.  A reviewer that can edit the branch it is reviewing is a
    reviewer nobody can trust, and it would race the agent that owns the MR.
    """
    base_tools: list[Tool] = [
        ThinkTool(),
        ViewTool(options=local_tool_options),
        SearchTextTool(options=local_tool_options),
        GetCWDTool(options=local_tool_options),
        GetPackageInfoTool(options=local_tool_options),
        GitLogSearchTool(options=local_tool_options),
        # Read-only in effect: it only ever runs in the throwaway clone, and
        # the prompt forbids mutating commands.
        RunShellCommandTool(options=local_tool_options),
        # `rpmbuild -bp` — proves the patches in the MR actually apply.
        RunPackagePrepTool(options=local_tool_options),
    ]
    base_tools.extend([t for t in mcp_tools_list if t.name in _ALLOWED_MCP_TOOLS])

    return ReasoningAgent(
        name="MRVerificationAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=base_tools,
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
        role="Red Hat Enterprise Linux package maintainer reviewing a merge request",
        instructions=render_template("mr_verification/instructions.j2"),
    )


def format_review_comment(result: MRVerificationOutputSchema, source_agent: str) -> str:
    """Render the structured review as the Markdown comment posted on the MR."""
    if result.verdict is MRVerificationVerdict.APPROVED:
        headline = "## ✅ Ymir MR review: no problems found"
    elif result.verdict is MRVerificationVerdict.CHANGES_REQUESTED:
        headline = "## 🛑 Ymir MR review: changes requested"
    else:
        headline = "## ❔ Ymir MR review: inconclusive"

    parts = [
        headline,
        "",
        f"An automated review of this {source_agent.lower()} MR by Ymir. "
        "It is advisory — a human maintainer still owns the merge decision, "
        "and the reviewer can be wrong in both directions.",
        "",
        result.summary.strip(),
        "",
    ]

    if result.findings:
        parts.append("### Findings")
        parts.append("")
        for finding in result.findings:
            icon = _SEVERITY_ICON.get(finding.severity, "•")
            location = f" (`{finding.file}`)" if finding.file else ""
            parts.append(f"- {icon} **{finding.severity.value}** [{finding.category}]{location}")
            parts.append(f"  {finding.description.strip()}")
            if finding.suggestion:
                parts.append(f"  _Suggested fix:_ {finding.suggestion.strip()}")
        parts.append("")

    if result.checks_performed:
        parts.append("<details><summary>Checks performed</summary>\n")
        parts.extend(f"- {check}" for check in result.checks_performed)
        parts.append("\n</details>")
        parts.append("")

    parts.append(
        "---\n"
        "> This review was produced by an AI agent and may be wrong. "
        "Disable it for this package by setting `verification.verify_mrs: false` in "
        "`gitlab.com/redhat/centos-stream/rules/<package>/ymir.yaml`."
    )
    return "\n".join(parts)


class VerificationState(BaseModel):
    """Workflow state for the MR verification agent."""

    merge_request_url: str
    package: str | None = Field(default=None)
    dist_git_branch: str | None = Field(default=None)
    jira_issue: str | None = Field(default=None)
    cve_id: str | None = Field(default=None)
    source_agent: str = Field(default="Backport")
    local_clone: Path | None = Field(default=None)
    head_sha: str | None = Field(default=None)
    merge_request_title: str | None = Field(default=None)
    merge_request_description: str | None = Field(default=None)
    changed_files: list[str] = Field(default_factory=list)
    diff: str = Field(default="")
    diff_truncated: bool = Field(default=False)
    failed_pipeline_jobs: str | None = Field(default=None)
    sources_available: bool = Field(default=False)
    block_on_findings: bool = Field(default=False)
    result: MRVerificationOutputSchema | None = Field(default=None)
    skipped: bool = Field(default=False)


async def run_workflow(
    merge_request_url: str,
    package: str | None = None,
    dist_git_branch: str | None = None,
    jira_issue: str | None = None,
    cve_id: str | None = None,
    source_agent: str = "Backport",
    redis_conn=None,
    dry_run: bool = False,
    verification_agent_factory=None,
) -> VerificationState:
    """Review one merge request and publish the verdict.

    Args:
        merge_request_url: MR to review.
        package: RPM package name; re-read from the MR when omitted.
        dist_git_branch: Target branch; re-read from the MR when omitted.
        jira_issue: Primary Jira issue; parsed from the MR description when omitted.
        cve_id: CVE the MR fixes, when known.
        source_agent: Agent that opened the MR — drives the checklist emphasis.
        redis_conn: Redis connection; ``None`` in direct mode (disables the
            already-reviewed dedup and the completed-list push).
        dry_run: Skip the MR comment, MR labels and Jira comment.
        verification_agent_factory: Test seam for agent creation.
    """
    local_tool_options: dict[str, Any] = {"working_directory": None}
    if mock_env := get_mock_local_tool_env(jira_issue or "mr-verification"):
        local_tool_options["env"] = mock_env

    async with mcp_tools(
        os.environ["MCP_GATEWAY_URL"],
        call_meta={"jira_issue": jira_issue} if jira_issue else None,
    ) as gateway_tools:
        if verification_agent_factory:
            maybe = verification_agent_factory(gateway_tools, local_tool_options)
            verification_agent = await maybe if asyncio.iscoroutine(maybe) else maybe
        else:
            verification_agent = create_verification_agent(gateway_tools, local_tool_options)

        workflow = Workflow(VerificationState, name="MRVerificationWorkflow")

        async def prepare_clone(state):
            state.local_clone, mr_details, _ = await tasks.prepare_dist_git_from_merge_request(
                merge_request_url=state.merge_request_url,
                available_tools=gateway_tools,
            )
            state.package = mr_details.target_repo_name
            state.dist_git_branch = mr_details.target_branch
            state.merge_request_title = mr_details.title
            state.merge_request_description = mr_details.description
            if not state.jira_issue:
                # A missing Jira key is a finding, not a crash: an MR with no
                # traceable issue is exactly the kind of thing to report.
                try:
                    from ymir.agents.merge_request_agent import extract_jira_issue

                    state.jira_issue = extract_jira_issue(mr_details.description)
                except Exception as e:
                    logger.warning("Could not extract Jira issue from %s: %s", state.merge_request_url, e)
            if state.jira_issue:
                current_jira_issue.set(state.jira_issue)

            local_tool_options["working_directory"] = state.local_clone

            _, head_sha, _ = await run_subprocess(
                ["git", "rev-parse", "HEAD"],
                cwd=state.local_clone,
                env=local_tool_options.get("env"),
            )
            state.head_sha = (head_sha or "").strip() or None

            if await already_reviewed(redis_conn, state.merge_request_url, state.head_sha):
                logger.info(
                    "MR %s already reviewed at %s, skipping",
                    state.merge_request_url,
                    state.head_sha[:12],
                )
                state.skipped = True
                return Workflow.END

            try:
                config = await tasks.fetch_verification_config(state.package, gateway_tools)
            except tasks.InvalidVerificationConfigError as e:
                logger.warning("Ignoring malformed verification config for %s: %s", state.package, e)
                config = None
            if config is not None:
                if not config.verify_mrs:
                    logger.info("Verification disabled for %s, skipping", state.package)
                    state.skipped = True
                    return Workflow.END
                state.block_on_findings = config.block_on_findings

            return "collect_evidence"

        async def collect_evidence(state):
            git_env = local_tool_options.get("env")
            namespace = "centos-stream" if is_cs_branch(state.dist_git_branch) else "rhel"
            target_repo = f"https://gitlab.com/redhat/{namespace}/rpms/{state.package}"

            # The instructions tell the reviewer to prove the patches apply with
            # `run_package_prep`, and prep needs the source tarballs that the
            # clone does not carry.  Without this the agent burns a long time
            # trying to fetch them by hand (the lesson of PR #677).  Best-effort:
            # a package whose sources will not download is still reviewable by
            # reading the diff, it just cannot have its prep checked.
            try:
                await run_tool(
                    "download_sources",
                    dist_git_path=str(state.local_clone),
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    available_tools=gateway_tools,
                )
                state.sources_available = True
            except Exception as e:
                logger.warning("Could not download sources for %s: %s", state.package, e)
                state.sources_available = False

            exit_code, _, _ = await run_subprocess(
                ["git", "rev-parse", "--verify", f"refs/heads/{state.dist_git_branch}"],
                cwd=state.local_clone,
                env=git_env,
            )
            if exit_code != 0:
                await run_tool(
                    "fetch_branch",
                    repository=target_repo,
                    branch=state.dist_git_branch,
                    clone_path=str(state.local_clone),
                    available_tools=gateway_tools,
                )
                logger.info("Waiting %ds for NFS attribute cache to expire after MCP fetch", _NFS_CACHE_WAIT)
                await asyncio.sleep(_NFS_CACHE_WAIT)

            diff_range = f"{state.dist_git_branch}...HEAD"
            exit_code, names, names_err = await run_subprocess(
                ["git", "diff", "--name-only", diff_range],
                cwd=state.local_clone,
                env=git_env,
            )
            if exit_code != 0:
                state.result = MRVerificationOutputSchema(
                    verdict=MRVerificationVerdict.INCONCLUSIVE,
                    summary="Could not diff the MR branch against its target branch.",
                    error=f"git diff {diff_range} failed (exit {exit_code}): {(names_err or '').strip()}",
                )
                return "publish_verdict"
            state.changed_files = [f for f in (names or "").splitlines() if f.strip()]

            _, diff, _ = await run_subprocess(
                ["git", "diff", diff_range],
                cwd=state.local_clone,
                env=git_env,
            )
            diff = diff or ""
            if len(diff) > _MAX_DIFF_CHARS:
                diff = diff[:_MAX_DIFF_CHARS]
                state.diff_truncated = True
            state.diff = diff

            try:
                jobs = await run_tool(
                    "get_failed_pipeline_jobs_from_merge_request",
                    merge_request_url=state.merge_request_url,
                    available_tools=gateway_tools,
                )
                jobs_parsed = json.loads(jobs) if isinstance(jobs, str) else jobs
                state.failed_pipeline_jobs = json.dumps(jobs_parsed, indent=2) if jobs_parsed else None
            except Exception as e:
                # A missing or still-running pipeline is normal right after the
                # MR is opened; it must not sink the whole review.
                logger.info("No failed pipeline jobs available for %s: %s", state.merge_request_url, e)
                state.failed_pipeline_jobs = None

            return "run_verification_agent"

        async def run_verification_agent(state):
            prompt = render_template(
                "mr_verification/prompt.j2",
                MRVerificationInputSchema(
                    local_clone=state.local_clone,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    jira_issue=state.jira_issue or "unknown",
                    cve_id=state.cve_id,
                    source_agent=state.source_agent,
                    merge_request_url=state.merge_request_url,
                    merge_request_title=state.merge_request_title or "",
                    merge_request_description=state.merge_request_description or "",
                    changed_files=state.changed_files,
                    diff=state.diff,
                    diff_truncated=state.diff_truncated,
                    sources_available=state.sources_available,
                    failed_pipeline_jobs=state.failed_pipeline_jobs,
                ),
            )
            try:
                response = await verification_agent.run(
                    prompt,
                    expected_output=MRVerificationOutputSchema,
                    **get_agent_execution_config(),
                )
                state.result = MRVerificationOutputSchema.model_validate_json(response.last_message.text)
            except FrameworkError as e:
                logger.error("Verification agent error: %s", e)
                state.result = MRVerificationOutputSchema(
                    verdict=MRVerificationVerdict.INCONCLUSIVE,
                    summary="The automated review could not be completed.",
                    error=str(e),
                )
            except Exception as e:
                logger.error("Unexpected verification error: %s", e)
                state.result = MRVerificationOutputSchema(
                    verdict=MRVerificationVerdict.INCONCLUSIVE,
                    summary="The automated review could not be completed.",
                    error=str(e),
                )
            return "publish_verdict"

        async def publish_verdict(state):
            result = state.result
            if dry_run:
                logger.info("DRY_RUN: would post review %s", result.model_dump_json(indent=4))
                return "record_result"

            comment = format_review_comment(result, state.source_agent)
            blocking = state.block_on_findings and result.has_blockers
            try:
                if blocking:
                    await run_tool(
                        "add_blocking_merge_request_comment",
                        merge_request_url=state.merge_request_url,
                        comment=comment,
                        available_tools=gateway_tools,
                    )
                else:
                    await tasks.comment_in_mr(
                        merge_request_url=state.merge_request_url,
                        comment_text=comment,
                        available_tools=gateway_tools,
                    )
            except Exception as e:
                logger.warning("Failed to post review comment on %s: %s", state.merge_request_url, e)

            label = (
                JiraLabels.MR_CHANGES_REQUESTED.value
                if result.verdict is MRVerificationVerdict.CHANGES_REQUESTED
                else JiraLabels.MR_VERIFIED.value
            )
            if result.verdict is not MRVerificationVerdict.INCONCLUSIVE:
                try:
                    await run_tool(
                        "add_merge_request_labels",
                        merge_request_url=state.merge_request_url,
                        labels=[label],
                        available_tools=gateway_tools,
                    )
                except Exception as e:
                    logger.warning("Failed to label %s: %s", state.merge_request_url, e)

            if result.verdict is MRVerificationVerdict.CHANGES_REQUESTED and state.jira_issue:
                try:
                    await tasks.comment_in_jira(
                        jira_issue=state.jira_issue,
                        agent_type="MRVerification",
                        comment_text=(
                            f"Automated review of {state.merge_request_url} requested changes:\n\n"
                            f"{result.summary}"
                        ),
                        available_tools=gateway_tools,
                    )
                except Exception as e:
                    logger.warning("Failed to comment on %s: %s", state.jira_issue, e)

            return "record_result"

        async def record_result(state):
            if redis_conn is None:
                return Workflow.END
            await record_reviewed_head(redis_conn, state.merge_request_url, state.head_sha, dry_run)
            try:
                await fix_await(
                    redis_conn.lpush(
                        RedisQueues.COMPLETED_MR_VERIFICATION_LIST.value,
                        state.result.model_dump_json(),
                    )
                )
            except Exception as e:
                logger.warning("Failed to push completed verification result: %s", e)
            return Workflow.END

        workflow.add_step("prepare_clone", prepare_clone)
        workflow.add_step("collect_evidence", collect_evidence)
        workflow.add_step("run_verification_agent", run_verification_agent)
        workflow.add_step("publish_verdict", publish_verdict)
        workflow.add_step("record_result", record_result)

        initial_state = VerificationState(
            merge_request_url=merge_request_url,
            package=package,
            dist_git_branch=dist_git_branch,
            jira_issue=jira_issue,
            cve_id=cve_id,
            source_agent=source_agent,
        )
        try:
            response = await workflow.run(initial_state)
            return response.state
        finally:
            # The clone exists only to read the MR; nothing downstream needs it.
            if clone := local_tool_options.get("working_directory"):
                shutil.rmtree(clone, ignore_errors=True)
            if builddir := local_tool_options.get("builddir"):
                shutil.rmtree(builddir, ignore_errors=True)


async def main() -> None:
    """Entry point for the MR verification agent."""
    init_sentry()
    configure_logging(level=logging.INFO, buffer_size=int(os.getenv("LOG_BUFFER_SIZE", 0)))
    resolve_chat_model_override("mr_verification")

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])
    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    if merge_request_url := os.getenv("MERGE_REQUEST_URL"):
        logger.info("Running in direct mode for %s", merge_request_url)
        with span_processor.start_transaction(os.getenv("JIRA_ISSUE"), workflow="MRVerificationWorkflow"):
            state = await run_workflow(
                merge_request_url=merge_request_url,
                jira_issue=os.getenv("JIRA_ISSUE"),
                cve_id=os.getenv("CVE_ID"),
                source_agent=os.getenv("SOURCE_AGENT", "Backport"),
                dry_run=dry_run,
            )
            logger.info(
                "Direct run completed: %s",
                state.result.model_dump_json(indent=4) if state.result else "skipped",
            )
        return

    logger.info("Starting MR verification agent in queue mode")
    max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", 1))

    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        container_version = os.getenv("CONTAINER_VERSION", "c10s")
        queue = (
            RedisQueues.MR_VERIFICATION_QUEUE_C9S.value
            if container_version == "c9s"
            else RedisQueues.MR_VERIFICATION_QUEUE_C10S.value
        )
        queue_todo = RedisQueues.priority_twin(queue)
        redis_logger.info(
            "Connected to Redis, max retries set to %d, listening to queues: [%s, %s]",
            max_retries,
            queue_todo,
            queue,
        )

        async def process_task(payload: bytes) -> None:
            try:
                task = Task.model_validate_json(payload)
            except Exception as e:
                logger.error("Failed to parse task payload, skipping: %s", e)
                error_id = await fix_await(redis.incr(RedisQueues.ERROR_ID_COUNTER.value))
                entry = ErrorListEntry(
                    error_id=error_id,
                    error=ErrorData(details=f"Malformed task payload: {e}", jira_issue="unknown"),
                )
                await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, entry.model_dump_json()))
                return

            try:
                metadata = MRVerificationTaskMetadata.model_validate(task.metadata)
                current_jira_issue.set(metadata.jira_issue)
            except Exception as e:
                logger.error("Failed to parse task metadata, skipping: %s", e)
                error_id = await fix_await(redis.incr(RedisQueues.ERROR_ID_COUNTER.value))
                entry = ErrorListEntry(
                    error_id=error_id,
                    queue=queue_todo if task.user_triggered else queue,
                    task=task,
                    error=ErrorData(
                        details=f"Malformed task metadata: {e}",
                        jira_issue=task.metadata.get("jira_issue", "unknown"),
                    ),
                )
                await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, entry.model_dump_json()))
                return

            # Lock on the MR, not the issue: two MRs can resolve the same Jira
            # issue, and reviewing them concurrently is fine.  What must not
            # happen is two workers reviewing the *same* MR and double-posting.
            async with issue_lock(redis, metadata.merge_request_url, prefix="lock:mr-verify:") as token:
                if token is None:
                    logger.info(
                        "MR %s already being reviewed; dropping duplicate",
                        metadata.merge_request_url,
                    )
                    return
                await _process_verification_locked(task, metadata)

        async def _process_verification_locked(task: Task, metadata: MRVerificationTaskMetadata) -> None:
            retry_queue = queue_todo if task.user_triggered else queue
            try:
                with span_processor.start_transaction(metadata.jira_issue, workflow="MRVerificationWorkflow"):
                    state = await run_workflow(
                        merge_request_url=metadata.merge_request_url,
                        package=metadata.package,
                        dist_git_branch=metadata.dist_git_branch,
                        jira_issue=metadata.jira_issue,
                        cve_id=metadata.cve_id,
                        source_agent=metadata.source_agent,
                        redis_conn=redis,
                        dry_run=dry_run,
                    )
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error("Exception verifying %s: %s", metadata.merge_request_url, error)
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        "Verification failed (attempt %d/%d), re-queuing %s",
                        task.attempts,
                        max_retries,
                        metadata.merge_request_url,
                    )
                    await fix_await(redis.lpush(retry_queue, task.model_dump_json()))
                    return
                # Terminal: label the issue so the failure is visible, but never
                # block the MR — a reviewer that crashed says nothing about the MR.
                try:
                    await tasks.set_jira_labels(
                        jira_issue=metadata.jira_issue,
                        labels_to_add=[JiraLabels.MR_VERIFICATION_ERRORED.value],
                        labels_to_remove=[],
                        dry_run=dry_run,
                        user_triggered=task.user_triggered,
                    )
                except Exception as label_error:
                    logger.warning("Failed to set labels on %s: %s", metadata.jira_issue, label_error)
                error_id = await fix_await(redis.incr(RedisQueues.ERROR_ID_COUNTER.value))
                entry = ErrorListEntry(
                    error_id=error_id,
                    queue=retry_queue,
                    task=task,
                    error=ErrorData(details=error, jira_issue=metadata.jira_issue),
                )
                await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, entry.model_dump_json()))
                return

            if state.skipped:
                logger.info("Verification skipped for %s", metadata.merge_request_url)
            else:
                logger.info(
                    "Verification of %s completed: %s",
                    metadata.merge_request_url,
                    state.result.verdict.value if state.result else "no result",
                )

        shutdown_event = asyncio.Event()
        install_shutdown_handler(asyncio.get_running_loop(), shutdown_event)
        await run_task_loop(
            redis,
            [queue_todo, queue],
            process_task,
            max_concurrent=max_concurrent_tasks,
            shutdown_event=shutdown_event,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exception(e)
        sys.exit(1)
