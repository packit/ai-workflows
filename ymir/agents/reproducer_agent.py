import asyncio
import logging
import os
import sys
import traceback
from textwrap import dedent

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools.think import ThinkTool
from beeai_framework.utils.strings import to_json
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field

import ymir.agents.tasks as tasks
from ymir.agents.observability import setup_observability
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.tf_cleanup_middleware import TFReservationCleanupMiddleware
from ymir.agents.utils import (
    build_agent_factory_with_mock_repos,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    init_sentry,
    is_reasoning_enabled,
    mcp_tools,
    render_template,
    resolve_chat_model_override,
)
from ymir.common.base_utils import fix_await, redis_client
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.logging_setup import configure_logging
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
}

_PROMPT_TEMPLATE = "reproducer/prompt.j2"


# MCP tool names the reproducer agent needs access to
_REPRODUCER_MCP_TOOLS = [
    "get_jira_details",
    "get_patch_from_url",
    "get_maintainer_rules",
    "clone_repository",
    "fork_repository",
    "push_to_remote_repository",
    "open_merge_request",
    "add_merge_request_labels",
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
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
            ConditionalRequirement("get_jira_details", min_invocations=1),
            ConditionalRequirement("get_maintainer_rules", only_after=["get_jira_details"]),
            ConditionalRequirement(RunShellCommandTool, only_after=["get_jira_details"]),
            ConditionalRequirement("get_patch_from_url", only_after=["get_jira_details"]),
            ConditionalRequirement("clone_repository", only_after=["get_jira_details"]),
            ConditionalRequirement("reserve_testing_farm_machine", only_after=["get_jira_details"]),
        ],
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
    """Combined context for SKILL.md template rendering.

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
    if result.success:
        return JiraLabels.REPRODUCER_CREATED
    if result.not_reproducible_reason:
        return JiraLabels.REPRODUCER_NOT_REPRODUCIBLE
    return JiraLabels.REPRODUCER_FAILED


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

            output_schema_json = to_json(
                OutputSchema.model_json_schema(mode="validation"),
                indent=2,
                sort_keys=False,
            )
            response = await reproducer_agent.run(
                _render_prompt(agent_input, dry_run=dry_run),
                expected_output=dedent(
                    f"""
                    The final answer must be a JSON object matching the ReproducerOutputSchema.

                    **Important Formatting Rules:**
                    - The output must be a JSON object with the following keys:
                      `jira_issue`, `success`, `reproducer_type`, `test_mr_url`,
                      `testing_farm_request_id`, `pass_fail_criteria`, `summary`,
                      `not_reproducible_reason`.
                    - All string fields must be actual strings, not nested objects.

                    **Example for a successful reproducer:**
                    ```json
                    {{{{
                        "jira_issue": "RHEL-12345",
                        "success": true,
                        "reproducer_type": "cve",
                        "test_mr_url": "https://gitlab.com/redhat/rhel/tests/ksh/-/merge_requests/123",
                        "testing_farm_request_id": "tf-request-abc123",
                        "pass_fail_criteria": "PASS: program exits 0. FAIL: program crashes with SIGSEGV.",
                        "summary": "Created reproducer for CVE-2025-12345 in libfoo.",
                        "not_reproducible_reason": null
                    }}}}
                    ```

                    **Example for a non-reproducible result:**
                    ```json
                    {{{{
                        "jira_issue": "RHEL-12345",
                        "success": false,
                        "reproducer_type": "bug",
                        "test_mr_url": null,
                        "testing_farm_request_id": "tf-request-xyz789",
                        "pass_fail_criteria": "PASS: command completes within 10s. FAIL: command hangs.",
                        "summary": "Investigated RHEL-12345 but could not reproduce the bug.",
                        "not_reproducible_reason": "Race condition requires specific timing."
                    }}}}
                    ```

                    ```json
                    {output_schema_json}
                    ```
                    """
                ),
                **get_agent_execution_config(),
            )
            state.result = OutputSchema.model_validate_json(response.last_message.text)

            # Normalize jira_issue to upper-case
            state.result.jira_issue = state.result.jira_issue.upper()

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
        workflow.add_step("handle_results", handle_results)

        try:
            response = await workflow.run(ReproducerState(jira_issue=jira_issue))
            return response.state
        finally:
            await tf_cleanup.cleanup()


async def main() -> None:
    init_sentry()

    configure_logging(level=logging.INFO)
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
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        redis_logger.info(f"Connected to Redis, max retries set to {max_retries}")

        while True:
            redis_logger.info("Waiting for tasks from reproducer_queue (timeout: 30s)...")
            element = await fix_await(
                redis.brpop(
                    [RedisQueues.REPRODUCER_QUEUE_TODO.value, RedisQueues.REPRODUCER_QUEUE.value],
                    timeout=30,
                )
            )
            if element is None:
                redis_logger.info("No tasks received, continuing to wait...")
                continue

            _, payload = element
            redis_logger.info("Received task from queue")

            task = Task.model_validate_json(payload)
            input_data = InputSchema.model_validate(task.metadata)
            user_triggered = task.user_triggered
            logger.info(
                f"Processing reproducer for JIRA issue: {input_data.jira_issue}, "
                f"attempt: {task.attempts + 1}" + (" (user-triggered)" if user_triggered else "")
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
                continue

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
                    labels_to_remove=[
                        label
                        for label in JiraLabels.all_labels()
                        if label != JiraLabels.REPRODUCER_IN_PROGRESS.value
                    ],
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
                continue

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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
