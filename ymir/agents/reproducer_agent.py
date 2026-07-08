import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path

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
    run_tool,
)
from ymir.common.base_utils import fix_await, redis_client, run_task_loop
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.logging_setup import configure_logging, current_jira_issue
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    ErrorData,
    Task,
)
from ymir.common.models import (
    ReproducerInputSchema as InputSchema,
)
from ymir.common.models import (
    ReproducerOutputSchema as OutputSchema,
)
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
    return ReasoningAgent(
        name="ReproducerAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
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
        ],
    )


class _PromptContext(InputSchema):
    """Combined context for prompt template rendering.

    Extends the input schema with ``dry_run`` so the template can branch
    on it. Defined at module level to avoid re-creating the class on every
    ``_render_prompt`` call.
    """

    dry_run: bool = Field(default=False)


def _render_prompt(input_data: InputSchema, dry_run: bool = False) -> str:
    """Render the reproducer prompt template with the input schema fields."""
    context = _PromptContext(**input_data.model_dump(), dry_run=dry_run)
    return render_template(_PROMPT_TEMPLATE, context)


def _determine_result_label(result: OutputSchema) -> JiraLabels:
    """Map reproducer output to the appropriate Jira label."""
    if result.test_already_exists:
        return JiraLabels.REPRODUCER_ALREADY_EXISTS
    if result.success:
        return JiraLabels.REPRODUCER_CREATED
    if result.not_reproducible_reason:
        return JiraLabels.REPRODUCER_NOT_REPRODUCIBLE
    return JiraLabels.REPRODUCER_FAILED


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
):
    local_tool_options = None
    if mock_env := get_mock_local_tool_env(jira_issue):
        local_tool_options = {"env": mock_env}

    async with mcp_tools(os.getenv("MCP_GATEWAY_URL"), call_meta={"jira_issue": jira_issue}) as gateway_tools:
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
            """Fork, push, and open a merge request for verified reproducers."""
            result = state.result

            if not result.success:
                logger.info(f"Reproducer not successful for {state.jira_issue}, skipping MR creation")
                return "handle_results"

            if dry_run:
                logger.info(f"Dry run — skipping MR creation for {state.jira_issue}")
                return "handle_results"

            package = result.package
            tests_clone = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / f"tests-{package}"

            if not tests_clone.is_dir():
                logger.warning(f"Tests clone not found at {tests_clone}, skipping MR creation")
                result.summary += " (MR creation skipped: tests clone directory not found)"
                return "handle_results"

            # Determine test directory path within the clone
            if result.reproducer_type == "cve" and input_data and input_data.cve_id:
                test_dir = tests_clone / "Security" / input_data.cve_id
            else:
                test_dir = tests_clone / "Regression" / state.jira_issue

            if not test_dir.is_dir():
                logger.warning(f"Test dir not found at {test_dir}, skipping MR creation")
                result.summary += " (MR creation skipped: test directory not found)"
                return "handle_results"

            update_branch = f"reproducer/{state.jira_issue}"
            try:
                await check_subprocess(["git", "checkout", "-B", update_branch], cwd=tests_clone)

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

                agent_input = InputSchema(jira_issue=state.jira_issue) if input_data is None else input_data
                mr_title = f"{package}: add {result.reproducer_type} reproducer for {state.jira_issue}"
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
                    logger.info(f"Created reproducer MR: {mr_url}")
                else:
                    logger.warning(f"MR creation returned no URL for {state.jira_issue}")
                    result.summary += " (MR creation did not return a URL)"

            except Exception as e:
                logger.warning(f"Error creating reproducer MR for {state.jira_issue}: {e}")
                result.test_mr_url = None
                result.summary += f" (MR creation failed: {e})"

            return "handle_results"

        async def handle_results(state):
            """Set Jira labels and post a comment based on the result."""
            result = state.result
            logger.info(
                f"Reproducer result for {state.jira_issue}: "
                f"success={result.success}, type={result.reproducer_type}"
            )

            if dry_run:
                logger.info(f"Dry run — skipping Jira updates for {state.jira_issue}")
                return Workflow.END

            # Build a human-readable comment
            comment_parts = []
            if result.success:
                comment_parts.append("*Resolution*: reproduced")
            elif result.not_reproducible_reason:
                comment_parts.append("*Resolution*: not-reproducible")
            else:
                comment_parts.append("*Resolution*: error")

            comment_parts.append(f"*Reproducer Type*: {result.reproducer_type}")

            if result.testing_farm_request_id:
                comment_parts.append(f"*Testing Farm Request*: {result.testing_farm_request_id}")

            if result.test_mr_url:
                comment_parts.append(f"*Test MR*: {result.test_mr_url}")

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
            await tf_cleanup.cleanup()


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
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        redis_logger.info(f"Connected to Redis, max retries set to {max_retries}")

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
            current_labels = await tasks.get_jira_labels(input_data.jira_issue)
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

            async def retry(task, error, input_data=input_data, user_triggered=user_triggered):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {input_data.jira_issue}"
                    )
                    retry_queue = (
                        RedisQueues.REPRODUCER_QUEUE_TODO.value
                        if task.user_triggered
                        else RedisQueues.REPRODUCER_QUEUE.value
                    )
                    await fix_await(redis.lpush(retry_queue, task.model_dump_json()))
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

            # ymir_reproducer_in_progress is the dedup anchor for the next
            # fetcher sweep. If we cannot write it, we must not proceed —
            # otherwise the fetcher will re-enqueue this issue and a second
            # reproducer will run in parallel.
            try:
                await tasks.set_jira_labels(
                    jira_issue=input_data.jira_issue,
                    labels_to_add=[JiraLabels.REPRODUCER_IN_PROGRESS.value],
                    labels_to_remove=list(_REPRODUCER_TERMINAL_LABELS),
                    dry_run=dry_run,
                    user_triggered=user_triggered,
                    critical=True,
                )
                logger.info(f"Cleaned up existing labels for {input_data.jira_issue}")
                # Post acknowledgement comment for user-triggered runs now that
                # the in-progress label write succeeded. This prevents duplicate
                # comments if the critical label write were to fail.
                await tasks.post_user_ack_once(
                    task=task,
                    jira_issue=input_data.jira_issue,
                    agent_type="Reproducer",
                    comment_text=(
                        "Ymir picked up your request and started processing. "
                        "Results will be posted here when reproducer analysis completes."
                    ),
                    user_triggered=user_triggered,
                    dry_run=dry_run,
                )
            except Exception as e:
                logger.error(
                    f"Could not set {JiraLabels.REPRODUCER_IN_PROGRESS.value} on "
                    f"{input_data.jira_issue} after retries: {e}; re-queuing to avoid duplicate reproducer."
                )
                error_msg = f"Failed to set in-progress label: {e}"
                error_data = ErrorData(details=error_msg, jira_issue=input_data.jira_issue)
                await retry(task, error_data.model_dump_json())
                # Long sleep on purpose: critical-write retries already burned
                # ~7s, so we're past transient blips. Typical Jira outages last
                # minutes; cycling faster just spams the API.
                await asyncio.sleep(60)
                return

            try:
                logger.info(f"Starting reproducer processing for {input_data.jira_issue}")
                with span_processor.start_transaction(input_data.jira_issue, workflow="reproducer"):
                    state = await run_workflow(
                        input_data.jira_issue,
                        dry_run,
                        create_reproducer_agent,
                        input_data=input_data,
                        user_triggered=user_triggered,
                    )
                    output = state.result
                    logger.info(
                        f"Reproducer processing completed for {input_data.jira_issue}, "
                        f"success: {output.success}"
                    )

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during reproducer processing for {input_data.jira_issue}: {error}")
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=input_data.jira_issue).model_dump_json(),
                )
            else:
                logger.info(f"Reproducer resolved as success={output.success} for {input_data.jira_issue}")

                # Push the completed result to the completed list
                await fix_await(
                    redis.lpush(
                        RedisQueues.COMPLETED_REPRODUCER_LIST.value,
                        output.model_dump_json(),
                    )
                )
                logger.info(
                    f"Pushed {input_data.jira_issue} to {RedisQueues.COMPLETED_REPRODUCER_LIST.value}"
                )

        await run_task_loop(
            redis,
            [RedisQueues.REPRODUCER_QUEUE_TODO.value, RedisQueues.REPRODUCER_QUEUE.value],
            process_task,
            max_concurrent=max_concurrent_tasks,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
