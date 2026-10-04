"""MR verification agent — watches the CI pipeline of Ymir-authored merge requests.

The scope is deliberately narrow: wait until the MR's pipeline reaches a
terminal state, and say something only when it *failed*.  The agent does not
review the change itself — not the patches, not the changelog, not the
``Release:`` bump, not spec-file hygiene.  Two reasons:

* Those artefacts are produced by near-deterministic steps.  Flagging them
  after the fact can only ask for the same step to run again, which produces
  the same output and the same complaint — a loop, not a review.
* Judging a spec change properly means judging it against the package's own
  rules, which maintainers set per package.  That knowledge belongs to the
  step that writes the change, not to a reviewer watching from outside.

What is left is the one signal no earlier step can produce for itself: what
the build and test pipeline did with the result.  This agent is a *reader*: it
never pushes, never edits the dist-git tree and never changes a Jira status.
Its only side effects are an MR label, a comment on the MR when CI failed, and
a Jira comment when the failure is attributable to the MR.

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
from datetime import UTC, datetime, timedelta
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
from ymir.common.delayed_queue import promote_due_tasks, schedule_task
from ymir.common.issue_lock import issue_lock
from ymir.common.logging_setup import configure_logging, current_jira_issue, get_trajectory_writeable
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    CIFailureAnalysis,
    CIVerificationInputSchema,
    CIVerificationVerdict,
    ErrorData,
    ErrorListEntry,
    FailedPipelineJob,
    MergeRequestPipelineStatus,
    MRVerificationOutputSchema,
    MRVerificationTaskMetadata,
    Task,
)
from ymir.common.utils import init_sentry
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.filesystem import GetCWDTool
from ymir.tools.unprivileged.text import SearchTextTool, ViewTool
from ymir.tools.unprivileged.wicked_git import GitLogSearchTool

logger = logging.getLogger(__name__)
redis_logger = logging.getLogger("agent.redis")

# The MCP gateway writes refs on its own pod; both pods share an NFS4 PVC whose
# attribute cache can serve stale directory listings for up to 30s.  Same
# constant, and the same reason, as in the consolidation agent.
_NFS_CACHE_WAIT = 60

# How long to wait between polls of a pipeline that is still running, and how
# long to keep waiting at all.  The deadline is stamped onto the task when it
# is first queued, so re-queued polls cannot extend it.
_POLL_INTERVAL = float(os.getenv("MR_VERIFICATION_POLL_INTERVAL", 300))
_MAX_WAIT = float(os.getenv("MR_VERIFICATION_MAX_WAIT", 6 * 3600))

# Per-MR key holding the pipeline that was last analysed.  Re-analysing the
# same pipeline costs a full LLM run and posts a duplicate comment.
# One key per MR rather than one big hash so the records expire on their own:
# the repo already has no eviction policy (see data_retention_policy.md) and a
# hash that only ever grows would be one more thing nobody prunes.
_ANALYSED_PIPELINE_PREFIX = "mr_verification_analysed_pipeline:"
_ANALYSED_PIPELINE_TTL = int(os.getenv("MR_VERIFICATION_ANALYSED_TTL", 90 * 24 * 3600))

# Job logs are the primary evidence and go straight into the prompt, so both
# the per-job size and the number of jobs are capped.  The agent can always
# pull more with `get_pipeline_job_log`.
_MAX_JOB_LOG_CHARS = int(os.getenv("MR_VERIFICATION_MAX_JOB_LOG_CHARS", 20_000))
_MAX_ANALYSED_JOBS = int(os.getenv("MR_VERIFICATION_MAX_FAILED_JOBS", 5))

# The diff is *context for attributing the failure*, not something under
# review, so it is capped far below what a review would need.
_MAX_DIFF_CHARS = int(os.getenv("MR_VERIFICATION_MAX_DIFF_CHARS", 20_000))

# Read-only MCP tools the analyst may use for corroborating evidence.
# `extract_log_snippets` is the Log Detective tool and is only present when
# the gateway managed to start that MCP server.
_ALLOWED_MCP_TOOLS = (
    "get_merge_request_pipeline_status",
    "get_failed_pipeline_jobs_from_merge_request",
    "get_pipeline_job_log",
    "download_artifacts",
    "extract_log_snippets",
    "get_jira_details",
)


def _analysed_pipeline_key(merge_request_url: str) -> str:
    return f"{_ANALYSED_PIPELINE_PREFIX}{merge_request_url}"


def pipeline_fingerprint(status: MergeRequestPipelineStatus) -> str | None:
    """Identify one pipeline run: a new push means a new id *and* a new SHA."""
    if status.pipeline_id is None:
        return None
    return f"{status.pipeline_id}:{status.sha or ''}"


async def already_handled(redis_conn, merge_request_url: str, fingerprint: str | None) -> bool:
    """Has this exact pipeline run already been handled?"""
    if redis_conn is None or not fingerprint:
        return False
    try:
        previous = await fix_await(redis_conn.get(_analysed_pipeline_key(merge_request_url)))
    except Exception as e:
        # Losing the dedup record costs a duplicate comment, not correctness.
        logger.warning("Could not read analysed pipeline for %s: %s", merge_request_url, e)
        return False
    if isinstance(previous, bytes):
        previous = previous.decode()
    return previous == fingerprint


async def record_handled_pipeline(
    redis_conn, merge_request_url: str, fingerprint: str | None, dry_run: bool = False
) -> None:
    """Remember the handled pipeline so the same result is not reported twice.

    A dry run publishes nothing, so recording it would suppress the real
    comment that follows.
    """
    if redis_conn is None or not fingerprint or dry_run:
        return
    try:
        await fix_await(
            redis_conn.set(_analysed_pipeline_key(merge_request_url), fingerprint, ex=_ANALYSED_PIPELINE_TTL)
        )
    except Exception as e:
        logger.warning("Failed to record analysed pipeline for %s: %s", merge_request_url, e)


def create_analysis_agent(
    mcp_tools_list: list[Tool],
    local_tool_options: dict[str, Any],
) -> ReasoningAgent:
    """Create the CI-failure analyst.

    The tool list is read-only — no ``create``/``str_replace``/``remove``, no
    ``git_patch_apply``, no push tool, no ``retry_pipeline_job``, and none of
    the Jira write tools.  An analyst that can edit the branch would race the
    agent that owns the MR; one that can retry jobs would paper over exactly
    the failures it is here to explain.
    """
    base_tools: list[Tool] = [
        ThinkTool(),
        ViewTool(options=local_tool_options),
        SearchTextTool(options=local_tool_options),
        GetCWDTool(options=local_tool_options),
        GitLogSearchTool(options=local_tool_options),
        # Read-only in effect: it only ever runs in the throwaway clone, and
        # the prompt forbids mutating commands.
        RunShellCommandTool(options=local_tool_options),
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
        role="Red Hat Enterprise Linux package maintainer triaging a failed CI pipeline",
        instructions=render_template("mr_verification/instructions.j2"),
    )


def format_ci_comment(result: MRVerificationOutputSchema) -> str:
    """Render the CI failure analysis as the Markdown comment posted on the MR."""
    if result.verdict is CIVerificationVerdict.CI_FAILED_MR:
        headline = "## 🛑 Ymir CI check: the pipeline failed because of this change"
    elif result.verdict is CIVerificationVerdict.CI_FAILED_INFRASTRUCTURE:
        headline = "## ⚠️ Ymir CI check: the pipeline failed, but not because of this change"
    else:
        headline = "## ❔ Ymir CI check: the pipeline failed and the cause is unclear"

    pipeline_link = f" ([pipeline]({result.pipeline_url}))" if result.pipeline_url else ""
    parts = [
        headline,
        "",
        f"An automated triage of this MR's CI failure by Ymir{pipeline_link}. "
        "It explains the failure; it does not review the change itself, and it can be wrong.",
        "",
        result.summary.strip(),
        "",
    ]

    if result.job_analyses:
        parts.append("### Failing jobs")
        parts.append("")
        for job in result.job_analyses:
            name = f"[{job.job_name}]({job.job_url})" if job.job_url else job.job_name
            blame = "caused by this MR" if job.caused_by_mr else "not caused by this MR"
            parts.append(f"- **{name}** — {job.category.value}, {blame}")
            if job.suggested_action:
                parts.append(f"  _Suggested action:_ {job.suggested_action.strip()}")
            if evidence := job.evidence.strip():
                parts.append("")
                parts.append("  <details><summary>Log evidence</summary>")
                parts.append("")
                parts.append(f"  ```\n{evidence}\n  ```")
                parts.append("")
                parts.append("  </details>")
            parts.append("")

    if result.all_failures_retryable:
        parts.append(
            "> Every failure above looks transient — retrying the pipeline is probably all this needs."
        )
        parts.append("")

    parts.append(
        "---\n"
        "> This analysis was produced by an AI agent and may be wrong. "
        "Disable it for this package by setting `verification.analyze_ci_failures: false` in "
        "`gitlab.com/redhat/centos-stream/rules/<package>/ymir.yaml`."
    )
    return "\n".join(parts)


def format_job_logs(logs: dict[str, str]) -> str:
    """Render the collected job logs as one block for the prompt."""
    if not logs:
        return "(no job logs could be read)"
    return "\n\n".join(f"### {name}\n```\n{log}\n```" for name, log in logs.items())


class VerificationState(BaseModel):
    """Workflow state for the MR CI verification agent."""

    merge_request_url: str
    package: str | None = Field(default=None)
    dist_git_branch: str | None = Field(default=None)
    jira_issue: str | None = Field(default=None)
    cve_id: str | None = Field(default=None)
    source_agent: str = Field(default="Backport")
    merge_request_title: str | None = Field(default=None)
    local_clone: Path | None = Field(default=None)
    pipeline: MergeRequestPipelineStatus | None = Field(default=None)
    failed_jobs: list[FailedPipelineJob] = Field(default_factory=list)
    job_logs: dict[str, str] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    diff: str = Field(default="")
    diff_truncated: bool = Field(default=False)
    result: MRVerificationOutputSchema | None = Field(default=None)
    # The pipeline has not finished: the caller should re-queue this job.
    wait_again: bool = Field(default=False)
    skipped: bool = Field(default=False)


async def run_workflow(
    merge_request_url: str,
    package: str | None = None,
    dist_git_branch: str | None = None,
    jira_issue: str | None = None,
    cve_id: str | None = None,
    source_agent: str = "Backport",
    deadline_at: datetime | None = None,
    redis_conn=None,
    dry_run: bool = False,
    analysis_agent_factory=None,
) -> VerificationState:
    """Check one merge request's CI pipeline and publish the outcome.

    Returns with ``wait_again=True`` when the pipeline has not finished yet;
    the caller decides when to come back. Nothing is published in that case.

    Args:
        merge_request_url: MR whose pipeline is being watched.
        package: RPM package name; re-read from the MR when omitted.
        dist_git_branch: Target branch; re-read from the MR when omitted.
        jira_issue: Primary Jira issue the MR resolves.
        cve_id: CVE the MR fixes, when known.
        source_agent: Agent that opened the MR — context for the analysis.
        deadline_at: Give up waiting for the pipeline after this moment.
        redis_conn: Redis connection; ``None`` in direct mode (disables the
            already-handled dedup and the completed-list push).
        dry_run: Skip the MR comment, MR labels and Jira comment.
        analysis_agent_factory: Test seam for agent creation.
    """
    local_tool_options: dict[str, Any] = {"working_directory": None}
    if mock_env := get_mock_local_tool_env(jira_issue or "mr-verification"):
        local_tool_options["env"] = mock_env

    async with mcp_tools(
        os.environ["MCP_GATEWAY_URL"],
        call_meta={"jira_issue": jira_issue} if jira_issue else None,
    ) as gateway_tools:
        workflow = Workflow(VerificationState, name="MRVerificationWorkflow")

        def _terminal(verdict: CIVerificationVerdict, summary: str, state) -> MRVerificationOutputSchema:
            pipeline = state.pipeline
            return MRVerificationOutputSchema(
                merge_request_url=state.merge_request_url,
                verdict=verdict,
                summary=summary,
                pipeline_status=pipeline.status if pipeline else None,
                pipeline_url=pipeline.web_url if pipeline else None,
                pipeline_sha=pipeline.sha if pipeline else None,
            )

        async def check_pipeline(state):
            if state.jira_issue:
                current_jira_issue.set(state.jira_issue)

            state.pipeline = await run_tool(
                "get_merge_request_pipeline_status",
                merge_request_url=state.merge_request_url,
                available_tools=gateway_tools,
                expected_output=MergeRequestPipelineStatus,
            )
            pipeline = state.pipeline
            logger.info(
                "Pipeline for %s is '%s' (%d failed job(s))",
                state.merge_request_url,
                pipeline.status,
                len(pipeline.failed_jobs),
            )

            if pipeline.is_pending:
                expired = deadline_at is not None and datetime.now(UTC) >= deadline_at
                if not expired:
                    state.wait_again = True
                    return Workflow.END
                state.result = _terminal(
                    CIVerificationVerdict.TIMED_OUT,
                    f"The pipeline was still '{pipeline.status}' when the wait budget ran out; "
                    "no conclusion was drawn.",
                    state,
                )
                return "record_result"

            # Terminal from here on. Dedup covers pass and fail alike: a
            # re-delivered task must not re-label or re-comment.
            if await already_handled(redis_conn, state.merge_request_url, pipeline_fingerprint(pipeline)):
                logger.info(
                    "Pipeline %s of %s already handled, skipping",
                    pipeline.pipeline_id,
                    state.merge_request_url,
                )
                state.skipped = True
                return Workflow.END

            if pipeline.is_passing:
                state.result = _terminal(
                    CIVerificationVerdict.CI_PASSED,
                    f"CI finished with status '{pipeline.status}'.",
                    state,
                )
                return "publish_verdict"

            if not pipeline.is_failed:
                # canceled, or something GitLab added since: terminal but not
                # a failure we can explain. Say nothing rather than guess.
                state.result = _terminal(
                    CIVerificationVerdict.INCONCLUSIVE,
                    f"CI ended with status '{pipeline.status}', which says nothing about the change.",
                    state,
                )
                return "record_result"

            state.failed_jobs = pipeline.blocking_failed_jobs or pipeline.failed_jobs
            if not state.failed_jobs:
                state.result = _terminal(
                    CIVerificationVerdict.INCONCLUSIVE,
                    "The pipeline is marked failed but reports no failing job.",
                    state,
                )
                return "publish_verdict"
            return "collect_failure_evidence"

        async def collect_failure_evidence(state):
            # Only reached when CI actually failed: the clone, the target-branch
            # fetch and its NFS wait are the expensive part of this agent, and
            # the common case (green pipeline) never pays for them.
            state.local_clone, mr_details, _ = await tasks.prepare_dist_git_from_merge_request(
                merge_request_url=state.merge_request_url,
                available_tools=gateway_tools,
            )
            state.package = mr_details.target_repo_name
            state.dist_git_branch = mr_details.target_branch
            state.merge_request_title = mr_details.title
            local_tool_options["working_directory"] = state.local_clone

            git_env = local_tool_options.get("env")
            namespace = "centos-stream" if is_cs_branch(state.dist_git_branch) else "rhel"
            target_repo = f"https://gitlab.com/redhat/{namespace}/rpms/{state.package}"

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
            if exit_code == 0:
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
            else:
                # The diff is context, not evidence: a failure to compute it
                # makes attribution harder, not impossible.
                logger.warning("Could not diff %s: %s", diff_range, (names_err or "").strip())

            for job in state.failed_jobs[:_MAX_ANALYSED_JOBS]:
                try:
                    state.job_logs[job.name] = await run_tool(
                        "get_pipeline_job_log",
                        project_url=target_repo,
                        job_id=int(job.id),
                        max_chars=_MAX_JOB_LOG_CHARS,
                        available_tools=gateway_tools,
                    )
                except Exception as e:
                    logger.warning("Could not read log of job %s (%s): %s", job.name, job.id, e)

            return "run_analysis_agent"

        async def run_analysis_agent(state):
            if analysis_agent_factory:
                maybe = analysis_agent_factory(gateway_tools, local_tool_options)
                analysis_agent = await maybe if asyncio.iscoroutine(maybe) else maybe
            else:
                analysis_agent = create_analysis_agent(gateway_tools, local_tool_options)

            prompt = render_template(
                "mr_verification/prompt.j2",
                CIVerificationInputSchema(
                    local_clone=state.local_clone,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    jira_issue=state.jira_issue or "unknown",
                    cve_id=state.cve_id,
                    source_agent=state.source_agent,
                    merge_request_url=state.merge_request_url,
                    merge_request_title=state.merge_request_title or "",
                    pipeline_url=state.pipeline.web_url if state.pipeline else None,
                    failed_jobs=json.dumps([job.model_dump() for job in state.failed_jobs], indent=2),
                    job_logs=format_job_logs(state.job_logs),
                    changed_files=state.changed_files,
                    diff=state.diff,
                    diff_truncated=state.diff_truncated,
                ),
            )
            try:
                response = await analysis_agent.run(
                    prompt,
                    expected_output=CIFailureAnalysis,
                    **get_agent_execution_config(),
                )
                analysis = CIFailureAnalysis.model_validate_json(response.last_message.text)
                state.result = MRVerificationOutputSchema(
                    merge_request_url=state.merge_request_url,
                    verdict=CIVerificationVerdict(analysis.verdict),
                    summary=analysis.summary,
                    pipeline_status=state.pipeline.status if state.pipeline else None,
                    pipeline_url=state.pipeline.web_url if state.pipeline else None,
                    pipeline_sha=state.pipeline.sha if state.pipeline else None,
                    job_analyses=analysis.job_analyses,
                )
            except Exception as e:
                # FrameworkError and anything else: a failed analysis is
                # reported as inconclusive, never as a verdict on the MR.
                logger.error("CI analysis error: %s", e)
                state.result = _terminal(
                    CIVerificationVerdict.INCONCLUSIVE,
                    "CI failed, but the automated analysis of the failure could not be completed.",
                    state,
                )
                state.result.error = str(e)
            return "publish_verdict"

        async def publish_verdict(state):
            result = state.result
            if dry_run:
                logger.info("DRY_RUN: would publish %s", result.model_dump_json(indent=4))
                return "record_result"

            label = {
                CIVerificationVerdict.CI_PASSED: JiraLabels.MR_CI_PASSED.value,
                CIVerificationVerdict.CI_FAILED_MR: JiraLabels.MR_CI_FAILED.value,
                CIVerificationVerdict.CI_FAILED_INFRASTRUCTURE: JiraLabels.MR_CI_INFRA_FAILURE.value,
            }.get(result.verdict)
            if label:
                try:
                    await run_tool(
                        "add_merge_request_labels",
                        merge_request_url=state.merge_request_url,
                        labels=[label],
                        available_tools=gateway_tools,
                    )
                except Exception as e:
                    logger.warning("Failed to label %s: %s", state.merge_request_url, e)

            # A green pipeline needs no comment: the MR widget already says so,
            # and a bot that congratulates every MR trains people to skim.
            if result.verdict is CIVerificationVerdict.CI_PASSED:
                return "record_result"
            if result.verdict in (CIVerificationVerdict.INCONCLUSIVE, CIVerificationVerdict.TIMED_OUT):
                return "record_result"

            try:
                await tasks.comment_in_mr(
                    merge_request_url=state.merge_request_url,
                    comment_text=format_ci_comment(result),
                    available_tools=gateway_tools,
                )
            except Exception as e:
                logger.warning("Failed to comment on %s: %s", state.merge_request_url, e)

            # Only the MR's own failures are the issue's business; an
            # infrastructure flake is noise on the Jira issue.
            if result.caused_by_mr and state.jira_issue:
                try:
                    await tasks.comment_in_jira(
                        jira_issue=state.jira_issue,
                        agent_type="MRVerification",
                        comment_text=(
                            f"CI failed on {state.merge_request_url} because of the change itself:"
                            f"\n\n{result.summary}"
                        ),
                        available_tools=gateway_tools,
                    )
                except Exception as e:
                    logger.warning("Failed to comment on %s: %s", state.jira_issue, e)

            return "record_result"

        async def record_result(state):
            if redis_conn is None:
                return Workflow.END
            await record_handled_pipeline(
                redis_conn,
                state.merge_request_url,
                pipeline_fingerprint(state.pipeline) if state.pipeline else None,
                dry_run,
            )
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

        workflow.add_step("check_pipeline", check_pipeline)
        workflow.add_step("collect_failure_evidence", collect_failure_evidence)
        workflow.add_step("run_analysis_agent", run_analysis_agent)
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
            # The clone exists only to attribute the failure; nothing
            # downstream needs it.
            if clone := local_tool_options.get("working_directory"):
                shutil.rmtree(clone, ignore_errors=True)


async def main() -> None:
    """Entry point for the MR verification agent."""
    init_sentry()
    configure_logging(level=logging.INFO, buffer_size=int(os.getenv("LOG_BUFFER_SIZE", 0)))
    resolve_chat_model_override("mr_verification")

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])
    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    if merge_request_url := os.getenv("MERGE_REQUEST_URL"):
        logger.info("Running in direct mode for %s", merge_request_url)
        deadline = datetime.now(UTC) + timedelta(seconds=_MAX_WAIT)
        with span_processor.start_transaction(os.getenv("JIRA_ISSUE"), workflow="MRVerificationWorkflow"):
            while True:
                # No Redis in direct mode, so the wait is an in-process sleep
                # instead of a delayed queue.
                state = await run_workflow(
                    merge_request_url=merge_request_url,
                    jira_issue=os.getenv("JIRA_ISSUE"),
                    cve_id=os.getenv("CVE_ID"),
                    source_agent=os.getenv("SOURCE_AGENT", "Backport"),
                    deadline_at=deadline,
                    dry_run=dry_run,
                )
                if not state.wait_again:
                    break
                logger.info("Pipeline still running, checking again in %.0fs", _POLL_INTERVAL)
                await asyncio.sleep(_POLL_INTERVAL)
            logger.info(
                "Direct run completed: %s",
                state.result.model_dump_json(indent=4) if state.result else "skipped",
            )
        return

    logger.info("Starting MR verification agent in queue mode")
    max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", 1))
    poll_timeout = int(os.getenv("MR_VERIFICATION_POLL_TIMEOUT", 30))

    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        queue = RedisQueues.MR_VERIFICATION_QUEUE.value
        queue_todo = RedisQueues.MR_VERIFICATION_QUEUE_TODO.value
        delayed_queue = RedisQueues.MR_VERIFICATION_DELAYED_QUEUE.value
        redis_logger.info(
            "Connected to Redis, max retries set to %d, listening to queues: [%s, %s] (+ %s)",
            max_retries,
            queue_todo,
            queue,
            delayed_queue,
        )

        def _target_queue_for_delayed_payload(payload: str) -> str:
            try:
                delayed_task = Task.model_validate_json(payload)
            except Exception:
                return queue
            return queue_todo if delayed_task.user_triggered else queue

        async def poll_verification():
            await promote_due_tasks(redis, delayed_queue, _target_queue_for_delayed_payload)
            return await fix_await(redis.brpop([queue_todo, queue], timeout=poll_timeout))

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
            # issue, and watching them concurrently is fine.  What must not
            # happen is two workers handling the *same* MR and double-posting.
            async with issue_lock(redis, metadata.merge_request_url, prefix="lock:mr-verify:") as token:
                if token is None:
                    logger.info(
                        "MR %s already being checked; dropping duplicate",
                        metadata.merge_request_url,
                    )
                    return
                await _process_verification_locked(task, metadata)

        async def reschedule(task: Task, metadata: MRVerificationTaskMetadata) -> None:
            """Put the job back on the delayed queue for the next poll.

            ``poll_count`` is tracked separately from ``Task.attempts``: waiting
            for CI is the job, not a failure, and must not eat retry budget.
            """
            metadata.poll_count += 1
            task.metadata = metadata.model_dump(mode="json")
            await schedule_task(redis, delayed_queue, task.model_dump_json(), _POLL_INTERVAL)
            logger.info(
                "Pipeline of %s still running (poll %d), next check in %.0fs",
                metadata.merge_request_url,
                metadata.poll_count,
                _POLL_INTERVAL,
            )

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
                        deadline_at=metadata.deadline_at,
                        redis_conn=redis,
                        dry_run=dry_run,
                    )
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error("Exception checking %s: %s", metadata.merge_request_url, error)
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        "CI verification failed (attempt %d/%d), re-queuing %s",
                        task.attempts,
                        max_retries,
                        metadata.merge_request_url,
                    )
                    await fix_await(redis.lpush(retry_queue, task.model_dump_json()))
                    return
                # Terminal: label the issue so the failure is visible, but never
                # touch the MR — a watcher that crashed says nothing about CI.
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

            if state.wait_again:
                await reschedule(task, metadata)
            elif state.skipped:
                logger.info("CI verification skipped for %s", metadata.merge_request_url)
            else:
                logger.info(
                    "CI verification of %s completed: %s",
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
            poll_fn=poll_verification,
            shutdown_event=shutdown_event,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exception(e)
        sys.exit(1)
