"""Opt-in, read-only Temporal triage analysis for the first migration phase."""

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from pydantic_ai import Agent, RunContext
from pydantic_ai.durable_exec.temporal import AgentPlugin, TemporalDurability
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from ymir.agents.triage_agent import TriageRunContext, create_triage_agent
from ymir.common.models import TriageOutputSchema
from ymir.common.utils import mcp_tools, run_tool


async def get_jira_details(ctx: RunContext[TriageRunContext], issue_key: str) -> dict:
    """Read Jira details, comments, and links through the configured MCP gateway."""
    async with mcp_tools(
        os.environ["MCP_GATEWAY_URL"], max_retries=1, call_meta={"jira_issue": ctx.deps.jira_issue}
    ) as tools:
        return await run_tool("get_jira_details", available_tools=tools, issue_key=issue_key)


agent: Agent[TriageRunContext, TriageOutputSchema]


@dataclass
class TemporalTriageInput:
    prompt: str
    context: TriageRunContext
    model_settings: dict[str, Any]
    request_limit: int = 8


@workflow.defn
class TemporalTriageWorkflow:
    @workflow.run
    async def run(self, request: TemporalTriageInput) -> TriageOutputSchema:
        result = await agent.run(
            request.prompt,
            deps=request.context,
            model_settings=cast(ModelSettings, request.model_settings),
            usage_limits=UsageLimits(request_limit=request.request_limit),
        )
        return result.output


def create_worker(client: Client, task_queue: str, model: Model) -> Worker:
    """Bind the model once for the lifetime of the local worker."""
    global agent
    agent = create_triage_agent(
        [],
        model=model,
        tools=[get_jira_details],
        capabilities=[
            TemporalDurability(
                activity_config={
                    "start_to_close_timeout": timedelta(seconds=15),
                    "retry_policy": RetryPolicy(maximum_attempts=2),
                },
                model_activity_config={"start_to_close_timeout": timedelta(minutes=3)},
            ),
        ],
    )
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[TemporalTriageWorkflow],
        plugins=[AgentPlugin(agent)],
        # Share the configured agent with the sandbox. Its I/O runs in activities.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(__name__)
        ),
        max_cached_workflows=0,
    )
