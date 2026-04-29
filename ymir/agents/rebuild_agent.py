import asyncio
import logging
import os
import sys
import traceback

from beeai_framework.errors import FrameworkError
from beeai_framework.workflows import Workflow
from pydantic import Field

import ymir.agents.tasks as tasks
from ymir.agents.constants import I_AM_YMIR, MR_DESCRIPTION_FOOTER
from ymir.agents.log_agent import create_log_agent
from ymir.agents.log_agent import get_prompt as get_log_prompt
from ymir.agents.observability import setup_observability
from ymir.agents.package_update_steps import PackageUpdateState
from ymir.agents.utils import (
    get_agent_execution_config,
    mcp_tools,
    render_prompt,
    run_subprocess,
)
from ymir.common.base_utils import fix_await, redis_client
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import (
    ErrorData,
    LogInputSchema,
    LogOutputSchema,
    RebuildData,
    RebuildOutputSchema,
    Task,
)

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    local_tool_options = {"working_directory": None}

    class State(PackageUpdateState):
        rebuild_success: bool = Field(default=False)
        rebuild_error: str | None = Field(default=None)
        dependency_issue: str | None = Field(default=None)
        dependency_component: str | None = Field(default=None)

    async def run_workflow(
        package,
        dist_git_branch,
        jira_issue,
        dependency_issue=None,
        dependency_component=None,
    ):
        local_tool_options["working_directory"] = None

        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            log_agent = create_log_agent(gateway_tools, local_tool_options)

            workflow = Workflow(State, name="RebuildWorkflow")

            async def change_jira_status(state):
                if not dry_run:
                    try:
                        await tasks.change_jira_status(
                            jira_issue=state.jira_issue,
                            status="In Progress",
                            available_tools=gateway_tools,
                        )
                    except Exception as status_error:
                        logger.warning(f"Failed to change status for {state.jira_issue}: {status_error}")
                else:
                    logger.info(f"Dry run: would change status of {state.jira_issue} to In Progress")
                return "fork_and_prepare_dist_git"

            async def fork_and_prepare_dist_git(state):
                (
                    state.local_clone,
                    state.update_branch,
                    state.fork_url,
                    _,
                ) = await tasks.fork_and_prepare_dist_git(
                    jira_issue=state.jira_issue,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    available_tools=gateway_tools,
                )
                local_tool_options["working_directory"] = state.local_clone
                return "update_release"

            async def update_release(state):
                try:
                    await tasks.update_release(
                        local_clone=state.local_clone,
                        package=state.package,
                        dist_git_branch=state.dist_git_branch,
                        rebase=False,
                    )
                except Exception as e:
                    logger.warning(f"Error updating release: {e}")
                    state.rebuild_success = False
                    state.rebuild_error = f"Could not update release: {e}"
                    return "comment_in_jira"
                return "run_log_agent"

            async def run_log_agent(state):
                if state.dependency_component:
                    summary = (
                        f"Rebuild of {state.package} for {state.jira_issue} "
                        f"against updated {state.dependency_component}. "
                        "The changelog entry and commit title MUST mention "
                        f"{state.dependency_component}."
                    )
                elif state.dependency_issue:
                    summary = (
                        f"Rebuild of {state.package} for {state.jira_issue} against updated dependency "
                        f"({state.dependency_issue})."
                    )
                else:
                    summary = (
                        f"Rebuild of {state.package} against updated dependencies for {state.jira_issue}."
                    )

                response = await log_agent.run(
                    render_prompt(
                        template=get_log_prompt(),
                        input=LogInputSchema(
                            jira_issue=state.jira_issue,
                            changes_summary=summary,
                        ),
                    ),
                    expected_output=LogOutputSchema,
                    **get_agent_execution_config(),
                )
                state.log_result = LogOutputSchema.model_validate_json(response.last_message.text)
                return "stage_changes"

            async def stage_changes(state):
                try:
                    await tasks.stage_changes(
                        local_clone=state.local_clone,
                        files_to_commit=[f"{state.package}.spec"],
                    )
                except Exception as e:
                    logger.warning(f"Error staging changes: {e}")
                    state.rebuild_success = False
                    state.rebuild_error = f"Could not stage changes: {e}"
                    return "comment_in_jira"
                return "commit_push_and_open_mr"

            async def commit_push_and_open_mr(state):
                try:
                    # Check if anything is actually staged
                    exit_code, _, _ = await run_subprocess(
                        ["git", "diff", "--cached", "--quiet"],
                        cwd=state.local_clone,
                    )
                    is_empty_commit = (
                        exit_code == 0
                    )  # exit code 0 means no staged changes, so commit would be empty

                    dep_lines = []
                    if state.dependency_component:
                        dep_lines.append(f"Dependency: {state.dependency_component}")
                    if state.dependency_issue:
                        dep_lines.append(f"Dependency issue: {state.dependency_issue}")
                    dep_text = "\n".join(dep_lines) + "\n" if dep_lines else ""
                    (
                        state.merge_request_url,
                        state.merge_request_newly_created,
                    ) = await tasks.commit_push_and_open_mr(
                        local_clone=state.local_clone,
                        commit_message=(
                            f"{state.log_result.title}\n\n"
                            f"{state.log_result.description}\n\n"
                            f"{dep_text}"
                            f"Resolves: {state.jira_issue}\n\n"
                            f"This commit was created {I_AM_YMIR}\n\n"
                            "Assisted-by: Ymir\n"
                        ),
                        fork_url=state.fork_url,
                        dist_git_branch=state.dist_git_branch,
                        update_branch=state.update_branch,
                        mr_title=state.log_result.title,
                        mr_description=(
                            f"{state.log_result.description}\n\n"
                            f"{dep_text}"
                            f"Resolves: {state.jira_issue}\n"
                            f"\n\n{MR_DESCRIPTION_FOOTER}"
                        ),
                        available_tools=gateway_tools,
                        commit_only=dry_run,
                        allow_empty=is_empty_commit,
                    )
                    state.rebuild_success = True
                except Exception as e:
                    logger.warning(f"Error committing and opening MR: {e}")
                    state.merge_request_url = None
                    state.rebuild_success = False
                    state.rebuild_error = f"Could not commit and open MR: {e}"
                return "comment_in_jira"

            async def comment_in_jira(state):
                if dry_run:
                    return Workflow.END
                if state.rebuild_success:
                    comment_text = (
                        state.merge_request_url
                        if state.merge_request_url
                        else "Rebuild completed successfully"
                    )
                else:
                    comment_text = f"Agent failed to perform a rebuild: {state.rebuild_error}"
                logger.info(f"Result to be put in Jira comment: {comment_text}")
                await tasks.comment_in_jira(
                    jira_issue=state.jira_issue,
                    agent_type="Rebuild",
                    comment_text=comment_text,
                    available_tools=gateway_tools,
                )
                return Workflow.END

            workflow.add_step("change_jira_status", change_jira_status)
            workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
            workflow.add_step("update_release", update_release)
            workflow.add_step("run_log_agent", run_log_agent)
            workflow.add_step("stage_changes", stage_changes)
            workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
            workflow.add_step("comment_in_jira", comment_in_jira)

            response = await workflow.run(
                State(
                    package=package,
                    dist_git_branch=dist_git_branch,
                    jira_issue=jira_issue,
                    dependency_issue=dependency_issue,
                    dependency_component=dependency_component,
                ),
            )
            return response.state

    # Direct mode: run with environment variables
    if (
        (package := os.getenv("PACKAGE", None))
        and (branch := os.getenv("BRANCH", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
    ):
        dependency_issue = os.getenv("DEPENDENCY_ISSUE", None)
        dependency_component = os.getenv("DEPENDENCY_COMPONENT", None)
        logger.info("Running in direct mode with environment variables")
        state = await run_workflow(
            package=package,
            dist_git_branch=branch,
            jira_issue=jira_issue,
            dependency_issue=dependency_issue,
            dependency_component=dependency_component,
        )
        logger.info(f"Direct run completed: success={state.rebuild_success}")
        return

    # Queue mode
    logger.info("Starting rebuild agent in queue mode")
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        container_version = os.getenv("CONTAINER_VERSION", "c10s")
        rebuild_queue = (
            RedisQueues.REBUILD_QUEUE_C9S.value
            if container_version == "c9s"
            else RedisQueues.REBUILD_QUEUE_C10S.value
        )
        logger.info(
            f"Connected to Redis, max retries set to {max_retries}, listening to queue: {rebuild_queue}"
        )

        while True:
            logger.info(f"Waiting for tasks from {rebuild_queue} (timeout: 30s)...")
            element = await fix_await(redis.brpop([rebuild_queue], timeout=30))
            if element is None:
                logger.info("No tasks received, continuing to wait...")
                continue

            _, payload = element
            logger.info("Received task from queue.")

            try:
                task = Task.model_validate_json(payload)
                triage_state = task.metadata
                rebuild_data = RebuildData.model_validate(triage_state["triage_result"]["data"])
                dist_git_branch = triage_state["target_branch"]
            except Exception as e:
                logger.error(f"Failed to parse task payload, skipping: {e}")
                await fix_await(
                    redis.lpush(
                        RedisQueues.ERROR_LIST.value,
                        ErrorData(
                            details=f"Malformed task payload: {e}", jira_issue="unknown"
                        ).model_dump_json(),
                    )
                )
                continue

            logger.info(
                f"Processing rebuild for package: {rebuild_data.package}, "
                f"JIRA: {rebuild_data.jira_issue}, branch: {dist_git_branch}, "
                f"attempt: {task.attempts + 1}"
            )

            async def retry(task, error, rebuild_data=rebuild_data):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {rebuild_data.jira_issue}"
                    )
                    await fix_await(redis.lpush(rebuild_queue, task.model_dump_json()))
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, "
                        f"moving to error list: {rebuild_data.jira_issue}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=rebuild_data.jira_issue,
                        labels_to_add=[JiraLabels.REBUILD_ERRORED.value],
                        labels_to_remove=[JiraLabels.TRIAGED_REBUILD.value],
                        dry_run=dry_run,
                    )
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            try:
                state = await run_workflow(
                    package=rebuild_data.package,
                    dist_git_branch=dist_git_branch,
                    jira_issue=rebuild_data.jira_issue,
                    dependency_issue=rebuild_data.dependency_issue,
                    dependency_component=rebuild_data.dependency_component,
                )
                logger.info(
                    f"Rebuild processing completed for {rebuild_data.jira_issue}, "
                    f"success: {state.rebuild_success}"
                )

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during rebuild processing for {rebuild_data.jira_issue}: {error}")
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=rebuild_data.jira_issue).model_dump_json(),
                )
            else:
                if state.rebuild_success:
                    logger.info(f"Rebuild successful for {rebuild_data.jira_issue}, adding to completed list")
                    await tasks.set_jira_labels(
                        jira_issue=rebuild_data.jira_issue,
                        labels_to_add=[JiraLabels.REBUILT.value],
                        labels_to_remove=[
                            JiraLabels.TRIAGED_REBUILD.value,
                            JiraLabels.REBUILD_ERRORED.value,
                            JiraLabels.REBUILD_FAILED.value,
                        ],
                        dry_run=dry_run,
                    )
                    await fix_await(
                        redis.lpush(
                            RedisQueues.COMPLETED_REBUILD_LIST.value,
                            RebuildOutputSchema(
                                success=True,
                                merge_request_url=state.merge_request_url,
                            ).model_dump_json(),
                        )
                    )
                else:
                    logger.warning(f"Rebuild failed for {rebuild_data.jira_issue}: {state.rebuild_error}")
                    await tasks.set_jira_labels(
                        jira_issue=rebuild_data.jira_issue,
                        labels_to_add=[JiraLabels.REBUILD_FAILED.value],
                        labels_to_remove=[JiraLabels.TRIAGED_REBUILD.value],
                        dry_run=dry_run,
                    )
                    await retry(
                        task,
                        ErrorData(
                            details=state.rebuild_error,
                            jira_issue=rebuild_data.jira_issue,
                        ).model_dump_json(),
                    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exception(e)
        sys.exit(1)
