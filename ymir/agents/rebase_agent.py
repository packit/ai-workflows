import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from pydantic import Field

import ymir.agents.tasks as tasks
from ymir.agents.build_agent import create_build_agent
from ymir.agents.build_agent import get_prompt as get_build_prompt
from ymir.agents.constants import I_AM_YMIR, ZSTREAM_TARGET_LABEL, mr_description_footer
from ymir.agents.log_agent import create_log_agent
from ymir.agents.log_agent import get_prompt as get_log_prompt
from ymir.agents.observability import setup_observability
from ymir.agents.package_update_steps import PackageUpdateState
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    format_mr_triage_details,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    init_sentry,
    is_reasoning_enabled,
    mcp_tools,
    render_template,
    resolve_chat_model_override,
    wrap_details,
)
from ymir.common.base_utils import fix_await, install_shutdown_handler, redis_client, run_task_loop
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.logging_setup import configure_logging, current_jira_issue, get_trajectory_writeable
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    BuildInputSchema,
    BuildOutputSchema,
    ErrorData,
    LogInputSchema,
    LogOutputSchema,
    RebaseData,
    RebaseInputSchema,
    RebaseOutputSchema,
    Task,
)
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.filesystem import GetCWDTool, RemoveTool
from ymir.tools.unprivileged.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    SearchTextTool,
    StrReplaceTool,
    ViewTool,
)
from ymir.tools.unprivileged.wicked_git import BuildSrpmTool, RunPackagePrepTool

logger = logging.getLogger(__file__)
redis_logger = logging.getLogger("agent.redis")


def get_instructions() -> str:
    return render_template("rebase/instructions.j2")


def get_prompt() -> str:
    return "rebase/prompt.j2"


def create_rebase_agent(mcp_tools: list[Tool], local_tool_options: dict[str, Any]) -> ReasoningAgent:
    return ReasoningAgent(
        name="RebaseAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            DuckDuckGoSearchTool(),
            RunShellCommandTool(options=local_tool_options),
            CreateTool(options=local_tool_options),
            ViewTool(options=local_tool_options),
            InsertTool(options=local_tool_options),
            InsertAfterSubstringTool(options=local_tool_options),
            StrReplaceTool(options=local_tool_options),
            SearchTextTool(options=local_tool_options),
            GetCWDTool(options=local_tool_options),
            RemoveTool(options=local_tool_options),
            RunPackagePrepTool(options=local_tool_options),
            BuildSrpmTool(options=local_tool_options),
        ]
        + [t for t in mcp_tools if t.name in ["upload_sources", "get_maintainer_rules"]],
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
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(),
    )


async def main() -> None:
    init_sentry()

    configure_logging(level=logging.INFO, buffer_size=int(os.getenv("LOG_BUFFER_SIZE", 0)))
    resolve_chat_model_override("rebase")

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    max_build_attempts = int(os.getenv("MAX_BUILD_ATTEMPTS", "10"))

    class State(PackageUpdateState):
        version: str
        fix_version: str | None = Field(default=None)
        justification: str | None = Field(default=None)
        triage_summary: str | None = Field(default=None)
        fedora_clone: Path | None = Field(default=None)
        leading_zstream_branch: str | None = Field(default=None)
        rebase_log: list[str] = Field(default_factory=list)
        rebase_result: RebaseOutputSchema | None = Field(default=None)
        attempts_remaining: int = Field(default=max_build_attempts)
        all_files_git_to_add: set[str] = Field(default_factory=set)
        abandon_autorelease: bool = Field(default=False)

    async def run_workflow(
        package,
        dist_git_branch,
        version,
        jira_issue,
        fix_version=None,
        justification=None,
        triage_summary=None,
        redis_conn=None,
        user_triggered=False,
    ):
        local_tool_options: dict[str, Any] = {"working_directory": None}
        if mock_env := get_mock_local_tool_env(jira_issue):
            local_tool_options["env"] = mock_env

        async with mcp_tools(
            os.environ["MCP_GATEWAY_URL"], call_meta={"jira_issue": jira_issue}
        ) as gateway_tools:
            rebase_agent = create_rebase_agent(gateway_tools, local_tool_options)
            log_agent = create_log_agent(gateway_tools, local_tool_options)

            workflow = Workflow(State, name="RebaseWorkflow")

            async def change_jira_status(state):
                if dry_run:
                    logger.info(f"Dry run: skipping Jira status change of {state.jira_issue} to In Progress")
                    return "fork_and_prepare_dist_git"
                # tasks.change_jira_status further gates the write on
                # JIRA_ALLOW_STATUS_CHANGES; nothing else to check here.
                try:
                    await tasks.change_jira_status(
                        jira_issue=state.jira_issue,
                        status="In Progress",
                        available_tools=gateway_tools,
                    )
                except Exception as status_error:
                    logger.warning(f"Failed to change status for {state.jira_issue}: {status_error}")
                return "fork_and_prepare_dist_git"

            async def fork_and_prepare_dist_git(state):
                (
                    state.local_clone,
                    state.update_branch,
                    state.fork_url,
                    state.fedora_clone,
                ) = await tasks.fork_and_prepare_dist_git(
                    jira_issue=state.jira_issue,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    available_tools=gateway_tools,
                    with_fedora=True,
                )
                local_tool_options["working_directory"] = state.local_clone
                state.leading_zstream_branch = await tasks.find_leading_zstream_branch(state.dist_git_branch)
                return "run_rebase_agent"

            async def run_rebase_agent(state):
                response = await rebase_agent.run(
                    render_template(
                        get_prompt(),
                        RebaseInputSchema(
                            local_clone=state.local_clone,
                            fedora_clone=state.fedora_clone,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            version=state.version,
                            jira_issue=state.jira_issue,
                            build_error=state.build_error,
                            triage_summary=state.triage_summary,
                            leading_zstream_branch=state.leading_zstream_branch,
                        ),
                    ),
                    expected_output=RebaseOutputSchema,
                    **get_agent_execution_config(),
                )
                state.rebase_result = RebaseOutputSchema.model_validate_json(response.last_message.text)
                if state.rebase_result.abandon_autorelease:
                    state.abandon_autorelease = True
                if state.rebase_result.success:
                    state.rebase_log.append(state.rebase_result.status)
                    # Accumulate files from this rebase iteration
                    if state.rebase_result.files_to_git_add:
                        state.all_files_git_to_add.update(state.rebase_result.files_to_git_add)
                    return "run_build_agent"
                return "comment_in_jira"

            async def run_build_agent(state):
                build_agent = create_build_agent(gateway_tools, local_tool_options)
                response = await build_agent.run(
                    render_template(
                        get_build_prompt(),
                        BuildInputSchema(
                            srpm_path=state.rebase_result.srpm_path,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                        ),
                    ),
                    expected_output=BuildOutputSchema,
                    **get_agent_execution_config(),
                )
                build_result = BuildOutputSchema.model_validate_json(response.last_message.text)
                if build_result.success:
                    return "update_release"
                if build_result.is_timeout:
                    logger.info(f"Build timed out for {state.jira_issue}, proceeding")
                    return "update_release"
                if build_result.is_infra_error:
                    logger.error(f"Copr infrastructure error for {state.jira_issue}: {build_result.error}")
                    state.rebase_result.success = False
                    state.rebase_result.error = build_result.error or "Copr API infrastructure error"
                    return "comment_in_jira"
                state.attempts_remaining -= 1
                if state.attempts_remaining <= 0:
                    state.rebase_result.success = False
                    state.rebase_result.error = (
                        f"Unable to successfully build the package in {max_build_attempts} attempts"
                    )
                    return "comment_in_jira"
                state.build_error = build_result.error
                return "fork_and_prepare_dist_git"

            async def update_release(state):
                try:
                    await tasks.update_release(
                        local_clone=state.local_clone,
                        package=state.package,
                        dist_git_branch=state.dist_git_branch,
                        rebase=True,
                        abandon_autorelease=state.abandon_autorelease,
                    )
                except Exception as e:
                    logger.warning(f"Error updating release: {e}")
                    state.rebase_result.success = False
                    state.rebase_result.error = f"Could not update release: {e}"
                    return "comment_in_jira"
                return "stage_changes"

            async def stage_changes(state):
                # Use accumulated files from all rebase iterations, fallback to *.spec if none specified
                files_to_git_add = list(state.all_files_git_to_add) or [f"{state.package}.spec"]

                try:
                    await tasks.stage_changes(
                        local_clone=state.local_clone,
                        files_to_commit=files_to_git_add,
                    )
                except Exception as e:
                    logger.warning(f"Error staging changes: {e}")
                    state.rebase_result.success = False
                    state.rebase_result.error = f"Could not stage changes: {e}"
                    return "comment_in_jira"
                if state.log_result:
                    return "commit_push_and_open_mr"
                return "run_log_agent"

            async def run_log_agent(state):
                response = await log_agent.run(
                    render_template(
                        get_log_prompt(),
                        LogInputSchema(
                            jira_issue=state.jira_issue,
                            changes_summary=state.rebase_log[-1],
                        ),
                    ),
                    expected_output=LogOutputSchema,
                    **get_agent_execution_config(),
                )
                log_output = LogOutputSchema.model_validate_json(response.last_message.text)

                if redis_conn and not dry_run:
                    # Cache MR metadata for sharing MR titles
                    # for the same package version across different streams if redis
                    # is available.
                    # Do not modify the cache during a dry run.
                    log_output = await tasks.cache_mr_metadata(
                        redis_conn,
                        log_output=log_output,
                        operation_type="rebase",
                        package=state.package,
                        details=state.version,
                    )
                state.log_result = log_output

                return "stage_changes"

            async def commit_push_and_open_mr(state):
                try:
                    triage_details_text = format_mr_triage_details(state.justification, state.triage_summary)
                    (
                        state.merge_request_url,
                        state.merge_request_newly_created,
                    ) = await tasks.commit_push_and_open_mr(
                        local_clone=state.local_clone,
                        commit_message=(
                            f"{state.log_result.title}\n\n"
                            f"{state.log_result.description}\n\n"
                            f"Resolves: {state.jira_issue}\n\n"
                            f"This commit was created {I_AM_YMIR}\n\n"
                            f"Assisted-by: Ymir\n"
                        ),
                        fork_url=state.fork_url,
                        dist_git_branch=state.dist_git_branch,
                        update_branch=state.update_branch,
                        mr_title=state.log_result.title,
                        mr_description=(
                            f"{state.log_result.description}\n\n"
                            f"{triage_details_text}"
                            f"Resolves: {state.jira_issue}\n\n"
                            f"{wrap_details('Rebase status', state.rebase_log[-1])}"
                            f"\n\n{mr_description_footer(state.package)}"
                        ),
                        available_tools=gateway_tools,
                        commit_only=dry_run,
                        labels=["ymir_rebase"]
                        + (
                            [ZSTREAM_TARGET_LABEL]
                            if await tasks.needs_zstream_target_label(
                                state.dist_git_branch, state.fix_version
                            )
                            else []
                        ),
                        package=state.package,
                    )
                except Exception as e:
                    logger.warning(f"Error committing and opening MR: {e}")
                    state.merge_request_url = None
                    state.rebase_result.success = False
                    state.rebase_result.error = f"Could not commit and open MR: {e}"
                return "comment_in_jira"

            async def comment_in_jira(state):
                if dry_run:
                    return Workflow.END
                if state.rebase_result.success:
                    comment_text = (
                        state.merge_request_url if state.merge_request_url else state.rebase_result.status
                    )
                    is_error = False
                else:
                    comment_text = f"Agent failed to perform a rebase: {state.rebase_result.error}"
                    is_error = True
                await tasks.comment_in_jira(
                    jira_issue=state.jira_issue,
                    agent_type="Rebase",
                    comment_text=comment_text,
                    is_error=is_error,
                    available_tools=gateway_tools,
                    user_triggered=user_triggered,
                )
                return Workflow.END

            workflow.add_step("change_jira_status", change_jira_status)
            workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
            workflow.add_step("run_rebase_agent", run_rebase_agent)
            workflow.add_step("run_build_agent", run_build_agent)
            workflow.add_step("update_release", update_release)
            workflow.add_step("stage_changes", stage_changes)
            workflow.add_step("run_log_agent", run_log_agent)
            workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
            workflow.add_step("comment_in_jira", comment_in_jira)

            response = await workflow.run(
                State(
                    package=package,
                    dist_git_branch=dist_git_branch,
                    version=version,
                    jira_issue=jira_issue,
                    fix_version=fix_version,
                    justification=justification,
                    triage_summary=triage_summary,
                ),
            )
            return response.state

    if (
        (package := os.getenv("PACKAGE", None))
        and (version := os.getenv("VERSION", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
        and (branch := os.getenv("BRANCH", None))
    ):
        logger.info("Running in direct mode with environment variables")
        with span_processor.start_transaction(jira_issue, workflow="rebase"):
            state = await run_workflow(
                package=package,
                dist_git_branch=branch,
                version=version,
                jira_issue=jira_issue,
                fix_version=os.getenv("FIX_VERSION"),
                justification=os.getenv("JUSTIFICATION", None),
                triage_summary=os.getenv("TRIAGE_SUMMARY", None),
                redis_conn=None,
            )
            logger.info(f"Direct run completed: {state.rebase_result.model_dump_json(indent=4)}")
            return

    logger.info("Starting rebase agent in queue mode")
    max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", 1))
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        # Determine which rebase queue to listen to based on container version
        container_version = os.getenv("CONTAINER_VERSION", "c10s")
        rebase_queue = (
            RedisQueues.REBASE_QUEUE_C9S.value
            if container_version == "c9s"
            else RedisQueues.REBASE_QUEUE_C10S.value
        )
        # Priority twin: ymir_todo-triggered tasks are served before normal ones.
        rebase_queue_todo = RedisQueues.priority_twin(rebase_queue)
        redis_logger.info(
            f"Connected to Redis, max retries set to {max_retries}, "
            f"listening to queues: [{rebase_queue_todo}, {rebase_queue}]"
        )

        async def process_task(payload):
            task = Task.model_validate_json(payload)
            triage_state = task.metadata
            rebase_data = RebaseData.model_validate(triage_state["triage_result"]["data"])
            current_jira_issue.set(rebase_data.jira_issue)
            dist_git_branch = triage_state["target_branch"]
            user_triggered = task.user_triggered
            logger.info(
                f"Processing rebase for package: {rebase_data.package}, "
                f"version: {rebase_data.version}, JIRA: {rebase_data.jira_issue}, "
                f"branch: {dist_git_branch}, attempt: {task.attempts + 1}"
                + (" (user-triggered via ymir_todo)" if user_triggered else "")
            )

            async def retry(
                task, error, comment_text=None, rebase_data=rebase_data, user_triggered=user_triggered
            ):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {rebase_data.jira_issue}"
                    )
                    retry_queue = rebase_queue_todo if task.user_triggered else rebase_queue
                    await fix_await(redis.lpush(retry_queue, task.model_dump_json()))
                else:
                    # Final attempt exhausted — mark errored and stop retrying.
                    logger.error(
                        f"Task failed after {max_retries} attempts, "
                        f"moving to error list: {rebase_data.jira_issue}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=rebase_data.jira_issue,
                        labels_to_add=[JiraLabels.REBASE_ERRORED.value],
                        labels_to_remove=[JiraLabels.TRIAGED_REBASE.value],
                        dry_run=dry_run,
                        user_triggered=user_triggered,
                    )
                    # Post failure feedback to Jira once, here on the final attempt
                    # only — never for intermediate retries. Restricted to
                    # user-triggered (ymir_todo) runs: a maintainer who didn't ask
                    # for processing shouldn't be notified, so skip the gateway
                    # connection entirely otherwise.
                    if user_triggered and comment_text and not dry_run:
                        try:
                            async with mcp_tools(
                                os.environ["MCP_GATEWAY_URL"],
                                call_meta={"jira_issue": rebase_data.jira_issue},
                            ) as gateway_tools:
                                await tasks.comment_in_jira(
                                    jira_issue=rebase_data.jira_issue,
                                    agent_type="Rebase",
                                    comment_text=comment_text,
                                    available_tools=gateway_tools,
                                    is_error=True,
                                    user_triggered=user_triggered,
                                )
                        except Exception as comment_error:
                            logger.warning(
                                f"Failed to post final rebase failure comment for "
                                f"{rebase_data.jira_issue}: {comment_error}"
                            )
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            try:
                logger.info(f"Starting rebase processing for {rebase_data.jira_issue}")
                with span_processor.start_transaction(rebase_data.jira_issue, workflow="rebase"):
                    state = await run_workflow(
                        package=rebase_data.package,
                        dist_git_branch=dist_git_branch,
                        version=rebase_data.version,
                        jira_issue=rebase_data.jira_issue,
                        fix_version=rebase_data.fix_version,
                        justification=rebase_data.justification,
                        triage_summary=rebase_data.triage_summary,
                        redis_conn=redis,
                        user_triggered=user_triggered,
                    )
                    logger.info(
                        f"Rebase processing completed for {rebase_data.jira_issue}, "
                        f"success: {state.rebase_result.success}"
                    )

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during rebase processing for {rebase_data.jira_issue}: {error}")
                reason = e.explain() if isinstance(e, FrameworkError) else e
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=rebase_data.jira_issue).model_dump_json(),
                    comment_text=f"Agent failed to perform a rebase: {reason}",
                )
            else:
                if state.rebase_result.success:
                    logger.info(f"Rebase successful for {rebase_data.jira_issue}, adding to completed list")
                    await tasks.set_jira_labels(
                        jira_issue=rebase_data.jira_issue,
                        labels_to_add=[JiraLabels.REBASED.value],
                        labels_to_remove=[
                            JiraLabels.TRIAGED_REBASE.value,
                            JiraLabels.REBASE_ERRORED.value,
                            JiraLabels.REBASE_FAILED.value,
                        ],
                        dry_run=dry_run,
                        user_triggered=user_triggered,
                    )
                    await fix_await(
                        redis.lpush(
                            RedisQueues.COMPLETED_REBASE_LIST.value,
                            state.rebase_result.model_dump_json(),
                        )
                    )
                else:
                    logger.warning(f"Rebase failed for {rebase_data.jira_issue}: {state.rebase_result.error}")
                    await tasks.set_jira_labels(
                        jira_issue=rebase_data.jira_issue,
                        labels_to_add=[JiraLabels.REBASE_FAILED.value],
                        labels_to_remove=[JiraLabels.TRIAGED_REBASE.value],
                        dry_run=dry_run,
                        user_triggered=user_triggered,
                    )
                    # No comment_text here: the in-workflow comment_in_jira step has
                    # already posted the failure feedback for this graceful path.
                    # Only the crash path (which never reaches that step) passes
                    # comment_text, so we never double-comment.
                    await retry(
                        task,
                        ErrorData(
                            details=getattr(state.rebase_result, "error", None) or "Unknown rebase error",
                            jira_issue=rebase_data.jira_issue,
                        ).model_dump_json(),
                    )

        shutdown_event = asyncio.Event()
        install_shutdown_handler(asyncio.get_running_loop(), shutdown_event)
        await run_task_loop(
            redis,
            [rebase_queue_todo, rebase_queue],
            process_task,
            max_concurrent=max_concurrent_tasks,
            shutdown_event=shutdown_event,
        )


if __name__ == "__main__":
    try:
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
