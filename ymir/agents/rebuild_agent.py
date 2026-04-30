import asyncio
import json
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
    ConsolidatedIssue,
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
        consolidated_issues: list[ConsolidatedIssue] = Field(default_factory=list)
        consolidation_summary: str | None = Field(default=None)

    async def run_workflow(
        package,
        dist_git_branch,
        jira_issue,
        dependency_issue=None,
        dependency_component=None,
        consolidated_issues=None,
        consolidation_summary=None,
    ):
        local_tool_options["working_directory"] = None

        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            log_agent = create_log_agent(gateway_tools, local_tool_options)

            workflow = Workflow(State, name="RebuildWorkflow")

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

            def _all_dependency_components(state):
                components = set()
                if state.dependency_component:
                    components.add(state.dependency_component)
                for item in state.consolidated_issues:
                    if item.dependency_component:
                        components.add(item.dependency_component)
                return sorted(components)

            def _all_dependency_issues(state):
                issues = set()
                if state.dependency_issue:
                    issues.add(state.dependency_issue)
                for item in state.consolidated_issues:
                    if item.dependency_issue:
                        issues.add(item.dependency_issue)
                return sorted(issues)

            async def run_log_agent(state):
                all_issues = [state.jira_issue] + [item.issue_key for item in state.consolidated_issues]
                issues_str = ", ".join(all_issues)
                dep_components = _all_dependency_components(state)

                if dep_components:
                    deps_str = ", ".join(dep_components)
                    summary = (
                        f"Rebuild of {state.package} for {issues_str} "
                        f"against updated {deps_str}. "
                        "The changelog entry and commit title MUST mention "
                        f"{deps_str}."
                    )
                else:
                    summary = f"Rebuild of {state.package} against updated dependencies for {issues_str}."

                response = await log_agent.run(
                    render_prompt(
                        template=get_log_prompt(),
                        input=LogInputSchema(
                            jira_issue=issues_str,
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

                    dep_components = _all_dependency_components(state)
                    if dep_components:
                        header = "Dependencies" if len(dep_components) > 1 else "Dependency"
                        dep_text = f"{header}: {', '.join(dep_components)}\n"
                    else:
                        dep_text = ""

                    dep_issues = _all_dependency_issues(state)
                    if dep_issues:
                        dep_issues_header = "Dependency issues" if len(dep_issues) > 1 else "Dependency issue"
                        dep_issues_text = f"{dep_issues_header}: {', '.join(dep_issues)}\n"
                    else:
                        dep_issues_text = ""

                    all_issues = [state.jira_issue] + [ci.issue_key for ci in state.consolidated_issues]
                    resolves_text = "Resolves: " + ", ".join(all_issues)

                    consolidation_text = ""
                    if state.consolidation_summary:
                        consolidation_text = (
                            f"\nSibling consolidation analysis:\n{state.consolidation_summary}\n"
                        )

                    (
                        state.merge_request_url,
                        state.merge_request_newly_created,
                    ) = await tasks.commit_push_and_open_mr(
                        local_clone=state.local_clone,
                        commit_message=(
                            f"{state.log_result.title}\n\n"
                            f"{state.log_result.description}\n\n"
                            f"{dep_text}"
                            f"{resolves_text}\n\n"
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
                            f"{dep_issues_text}"
                            f"{resolves_text}\n"
                            f"{consolidation_text}"
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
                    is_error = False
                else:
                    comment_text = f"Agent failed to perform a rebuild: {state.rebuild_error}"
                    is_error = True
                logger.info(f"Result to be put in Jira comment: {comment_text}")

                all_issues = [state.jira_issue] + [item.issue_key for item in state.consolidated_issues]
                for issue_key in all_issues:
                    try:
                        await tasks.comment_in_jira(
                            jira_issue=issue_key,
                            agent_type="Rebuild",
                            comment_text=comment_text,
                            is_error=is_error,
                            available_tools=gateway_tools,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to comment on issue {issue_key}: {e}")
                return Workflow.END

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
                    consolidated_issues=consolidated_issues or [],
                    consolidation_summary=consolidation_summary,
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
        consolidated_raw = os.getenv("CONSOLIDATED_ISSUES", None)
        consolidated_issues = json.loads(consolidated_raw) if consolidated_raw else None
        logger.info("Running in direct mode with environment variables")
        state = await run_workflow(
            package=package,
            dist_git_branch=branch,
            jira_issue=jira_issue,
            dependency_issue=dependency_issue,
            dependency_component=dependency_component,
            consolidated_issues=consolidated_issues,
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
                    for issue_key in rebuild_data.all_jira_issues:
                        try:
                            await tasks.set_jira_labels(
                                jira_issue=issue_key,
                                labels_to_add=[JiraLabels.REBUILD_ERRORED.value],
                                labels_to_remove=[JiraLabels.TRIAGED_REBUILD.value],
                                dry_run=dry_run,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to set labels on {issue_key}: {e}")
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            try:
                state = await run_workflow(
                    package=rebuild_data.package,
                    dist_git_branch=dist_git_branch,
                    jira_issue=rebuild_data.jira_issue,
                    dependency_issue=rebuild_data.dependency_issue,
                    dependency_component=rebuild_data.dependency_component,
                    consolidated_issues=rebuild_data.consolidated_issues,
                    consolidation_summary=rebuild_data.consolidation_summary,
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
                    for issue_key in rebuild_data.all_jira_issues:
                        try:
                            await tasks.set_jira_labels(
                                jira_issue=issue_key,
                                labels_to_add=[JiraLabels.REBUILT.value],
                                labels_to_remove=[
                                    JiraLabels.TRIAGED_REBUILD.value,
                                    JiraLabels.REBUILD_ERRORED.value,
                                    JiraLabels.REBUILD_FAILED.value,
                                ],
                                dry_run=dry_run,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to set labels on {issue_key}: {e}")
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
                    for issue_key in rebuild_data.all_jira_issues:
                        try:
                            await tasks.set_jira_labels(
                                jira_issue=issue_key,
                                labels_to_add=[JiraLabels.REBUILD_FAILED.value],
                                labels_to_remove=[JiraLabels.TRIAGED_REBUILD.value],
                                dry_run=dry_run,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to set labels on {issue_key}: {e}")
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
