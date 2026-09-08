import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool, ToolError
from beeai_framework.tools.think import ThinkTool
from beeai_framework.utils.cancellation import AbortController
from beeai_framework.workflows import Workflow
from pydantic import BaseModel

from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    render_template,
    run_tool,
)
from ymir.common.logging_setup import get_trajectory_writeable
from ymir.common.models import (
    BuildFailureAnalysisInput,
    BuildFailureAnalysisOutput,
    BuildInputSchema,
    BuildInstructionsInput,
    BuildOutputSchema,
    BuildResult,
)
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.filesystem import GetCWDTool
from ymir.tools.unprivileged.text import SearchTextTool, ViewTool

logger = logging.getLogger(__name__)


class BuildState(BaseModel):
    build_input: BuildInputSchema
    build_result: BuildResult | None = None
    output: BuildOutputSchema | None = None


async def run_build(
    *,
    build_input: BuildInputSchema,
    available_tools: list[Tool],
    local_tool_options: dict[str, Any],
) -> BuildOutputSchema:
    """Submit one build through the gateway, diagnosing only failures with logs.

    Copr submission retries and polling remain inside the existing tool. In
    particular, neither diagnosis nor a failed diagnosis resubmits the build.
    Callers retain ownership of timeout policy and workflow-level retries.
    A missing advertised build tool is reported as an infrastructure failure.
    """
    workflow = Workflow(BuildState, name="BuildWorkflow")

    async def execute_build(state: BuildState) -> str:
        try:
            result = await run_tool(
                "build_package",
                available_tools=available_tools,
                expected_output=BuildResult,
                **state.build_input.model_dump(mode="json"),
            )
        except ToolError as e:
            logger.warning("Copr build tool failed for %s: %s", state.build_input.jira_issue, e)
            state.output = BuildOutputSchema(success=False, error=str(e), is_infra_error=True)
            return Workflow.END

        state.build_result = result
        logger.info(
            "Copr build result for %s: success=%s, is_timeout=%s",
            state.build_input.jira_issue,
            result.success,
            result.is_timeout,
        )
        if result.success:
            state.output = BuildOutputSchema(success=True, error=None)
            return Workflow.END

        state.output = BuildOutputSchema(
            success=False,
            error=result.error_message or "Copr build failed without an error message",
            is_timeout=result.is_timeout,
        )
        if result.is_timeout:
            return Workflow.END

        if not any(urlsplit(url).path.endswith(".log.gz") for url in result.artifacts_urls or []):
            return Workflow.END

        return "diagnose_failure"

    async def diagnose_failure(state: BuildState) -> str:
        if state.build_result is None or state.output is None:
            raise RuntimeError("Build failure diagnosis requires an existing build result")
        try:
            analyst = create_build_failure_agent(available_tools, local_tool_options)
            response = await analyst.run(
                render_template(
                    get_prompt(),
                    BuildFailureAnalysisInput(
                        **state.build_input.model_dump(), build_result=state.build_result
                    ),
                ),
                expected_output=BuildFailureAnalysisOutput,
                **get_agent_execution_config(),
            )
            diagnosis = BuildFailureAnalysisOutput.model_validate_json(response.last_message.text)
            state.output.error = diagnosis.error
        except asyncio.CancelledError:
            # BeeAI AbortError also inherits Exception; preserve worker cancellation.
            raise
        except Exception:
            # A model outage or invalid answer cannot erase a known build failure.
            logger.exception(
                "Could not analyze failed build for %s; retaining tool error", state.build_input.jira_issue
            )
        return Workflow.END

    workflow.add_step("execute_build", execute_build)
    workflow.add_step("diagnose_failure", diagnose_failure)
    controller = AbortController()
    task = asyncio.ensure_future(
        workflow.run(BuildState(build_input=build_input), options={"signal": controller.signal})
    )
    try:
        response = await asyncio.shield(task)
    except asyncio.CancelledError:
        # BeeAI runs steps in a child task. Abort and drain that task before
        # returning cancellation so diagnosis cannot outlive its caller.
        controller.abort("Build workflow caller cancelled")
        await asyncio.gather(task, return_exceptions=True)
        raise
    if response.state.output is None:
        raise RuntimeError("Build workflow finished without a result")
    return response.state.output


def get_instructions(*, has_extract_log_snippets: bool = False) -> str:
    return render_template(
        "build/instructions.j2",
        BuildInstructionsInput(has_extract_log_snippets=has_extract_log_snippets),
    )


def get_prompt() -> str:
    return "build/prompt.j2"


def create_build_failure_agent(mcp_tools: list[Tool], local_tool_options: dict[str, Any]) -> ReasoningAgent:
    gateway_log_tools = {"download_artifacts", "extract_log_snippets"}
    has_gateway_log_tools = gateway_log_tools <= {t.name for t in mcp_tools}
    filtered_mcp_tools = (
        [t for t in mcp_tools if t.name in gateway_log_tools] if has_gateway_log_tools else []
    )

    requirements = [
        ConditionalRequirement(
            ThinkTool,
            force_at_step=1,
            force_after=Tool,
            consecutive_allowed=False,
            only_success_invocations=False,
        ),
    ]
    if has_gateway_log_tools:
        requirements.append(
            ConditionalRequirement("extract_log_snippets", only_after=["download_artifacts"]),
        )

    # Gateway-downloaded logs are not in the agent sandbox. Use that path only
    # when both tools are available; otherwise retrieve logs locally from URLs.
    local_tools = (
        []
        if has_gateway_log_tools
        else [
            RunShellCommandTool(options=local_tool_options),
            ViewTool(options=local_tool_options),
            SearchTextTool(options=local_tool_options),
            GetCWDTool(options=local_tool_options),
        ]
    )

    return ReasoningAgent(
        name="BuildFailureAnalyst",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            *local_tools,
            *filtered_mcp_tools,
        ],
        memory=UnconstrainedMemory(),
        requirements=requirements,
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(has_extract_log_snippets=has_gateway_log_tools),
    )
