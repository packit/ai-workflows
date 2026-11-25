"""Temporal orchestration for the read-only triage analysis stages."""

import os
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext, Tool
from pydantic_ai.durable_exec.temporal import AgentPlugin, TemporalDurability
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from ymir.agents import triage_agent as triage
from ymir.agents.cve_applicability_agent import APPLICABILITY_GATEWAY_TOOLS, create_triage_applicability_agent
from ymir.agents.pydantic_agent import invoke_pydantic_tool
from ymir.agents.triage_agent import PreparedApplicability, TriageRunContext, TriageState
from ymir.common.config import rhel_config_override
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import AssessedApplicabilityResult, CVEEligibilityResult, TriageOutputSchema
from ymir.common.temporal_triage import triage_issue_workflow_id as triage_issue_workflow_id
from ymir.common.utils import mcp_tools
from ymir.tools.privileged.jira import SetJiraFieldsToolInput
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.text import SearchTextTool, ViewTool
from ymir.tools.unprivileged.version_mapper import VersionMapperTool

RETRY_POLICY = RetryPolicy(maximum_attempts=2)


@dataclass
class TriageAnalysisRequest:
    jira_issue: str
    model_settings: dict[str, Any]
    request_limit: int
    force_cve_triage: bool = False
    user_triggered: bool = False
    triage_only_requested: bool = False
    follow_up_requested: bool = False
    config_override: dict[str, Any] | None = None
    collect_jira_fields: bool = False
    retry_operational_errors: bool = False
    defer_rebuild_check: bool = False


@dataclass
class TriageAnalysisResult:
    state: TriageState
    follow_up_requested: bool
    triage_usage: RunUsage | None = None
    jira_fields: tuple[SetJiraFieldsToolInput, ...] = ()
    coordination_workflow_id: str | None = None


@asynccontextmanager
async def _gateway(jira_issue: str, override: dict[str, Any] | None):
    with rhel_config_override(override):
        async with mcp_tools(
            os.environ["MCP_GATEWAY_URL"], max_retries=1, call_meta={"jira_issue": jira_issue}
        ) as tools:
            yield tools


@activity.defn
async def read_eligibility(
    jira_issue: str, override: dict | None, retry_operational_errors: bool = False
) -> CVEEligibilityResult:
    async with _gateway(jira_issue, override) as tools:
        result = await triage.read_cve_eligibility(jira_issue, tools)
    if retry_operational_errors and result.error and not triage.cve_eligibility_needs_input(result.error):
        raise ApplicationError(result.error, type="TriageEligibilityUnavailable")
    return result


async def _prepare_analysis(
    state: TriageState, override: dict | None, collect_jira_fields: bool
) -> tuple[TriageState, str, TriageRunContext]:
    async with _gateway(state.jira_issue, override) as tools:
        prompt, context = await triage.prepare_triage_analysis(
            state, tools, publish_jira_fields=collect_jira_fields
        )
    return state, prompt, replace(context, rhel_config_override=override)


@activity.defn
async def prepare_analysis(
    state: TriageState, override: dict | None
) -> tuple[TriageState, str, TriageRunContext]:
    return await _prepare_analysis(state, override, False)


@activity.defn
async def prepare_publication_analysis(
    state: TriageState, override: dict | None
) -> tuple[TriageState, str, TriageRunContext]:
    return await _prepare_analysis(state, override, True)


@activity.defn
async def normalize_version(state: TriageState, override: dict | None) -> TriageState:
    with rhel_config_override(override):
        await triage.normalize_triage_version(state)
    return state


@activity.defn
async def read_rebase_author(state: TriageState, override: dict | None) -> tuple[bool, str | None]:
    async with _gateway(state.jira_issue, override) as tools:
        return await triage.read_rebase_author(state, tools)


@activity.defn
async def resolve_target(state: TriageState, request: TriageAnalysisRequest) -> TriageState:
    async with _gateway(state.jira_issue, request.config_override) as tools:
        await triage.resolve_triage_target(
            state, tools, request.user_triggered, request.triage_only_requested
        )
    return state


def _applicability_workspace() -> Path:
    return Path(os.environ["GIT_REPO_BASEPATH"]) / "temporal-triage" / activity.info().workflow_run_id


@activity.defn
async def prepare_applicability(
    state: TriageState, override: dict | None
) -> tuple[TriageState, PreparedApplicability | None, str | None]:
    async with _gateway(state.jira_issue, override) as tools:
        prepared, next_step = await triage.prepare_cve_applicability(
            state, tools, workspace=_applicability_workspace()
        )
    return state, prepared, next_step


@activity.defn
async def cleanup_applicability(builddir: str | None) -> None:
    shutil.rmtree(_applicability_workspace(), ignore_errors=True)
    if builddir:
        shutil.rmtree(builddir, ignore_errors=True)


@activity.defn
async def read_buildroot(state: TriageState, override: dict | None) -> tuple[bool, str] | None:
    async with _gateway(state.jira_issue, override) as tools:
        return await triage.read_rebuild_buildroot(state, tools)


# The opt-in runner configures one worker per process. The same model and tool
# registration must be used when replacing a worker for an open execution.
triage_model_agent: Agent[TriageRunContext, TriageOutputSchema]
applicability_agent: Agent[TriageRunContext, AssessedApplicabilityResult]


async def _execute(fn, *args):
    return await workflow.execute_activity(
        fn, args=args, start_to_close_timeout=timedelta(minutes=10), retry_policy=RETRY_POLICY
    )


@workflow.defn(failure_exception_types=[ValueError])
class TriageAnalysisWorkflow:
    @workflow.run
    async def run(self, request: TriageAnalysisRequest) -> TriageAnalysisResult:
        info = workflow.info()
        owner_id = info.parent.workflow_id if info.parent else info.workflow_id
        if owner_id != triage_issue_workflow_id(request.jira_issue):
            raise ApplicationError("Start analysis under the Jira issue Workflow ID", non_retryable=True)

        state = TriageState(jira_issue=request.jira_issue)
        triage_usage = None
        jira_fields = ()
        override = request.config_override
        eligibility_args = [state.jira_issue, override]
        if request.retry_operational_errors:
            eligibility_args.append(True)
        state.cve_eligibility_result = await _execute(read_eligibility, *eligibility_args)
        step = triage.apply_cve_eligibility(state, request.force_cve_triage)

        if step == "run_triage_analysis":
            prepare = prepare_publication_analysis if request.collect_jira_fields else prepare_analysis
            state, prompt, context = await _execute(prepare, state, override)
            options = {
                "model_settings": cast(ModelSettings, request.model_settings),
                "usage_limits": UsageLimits(request_limit=request.request_limit),
            }
            answer = await triage_model_agent.run(prompt, deps=context, **options)
            triage_usage = answer.usage
            if request.collect_jira_fields:
                jira_fields = tuple(
                    SetJiraFieldsToolInput.model_validate(part.content)
                    for message in answer.new_messages()
                    for part in message.parts
                    if isinstance(part, ToolReturnPart) and part.tool_name == "set_jira_fields"
                )
            triage.apply_triage_output(state, answer.output)
            if getattr(answer.output.data, "fix_version", None):
                state = await _execute(normalize_version, state, override)
            step = triage.next_after_triage_output(state)

            if step == "verify_rebase_author":
                employee, status = await _execute(read_rebase_author, state, override)
                step = triage.apply_rebase_author(state, employee, status)

            if step == "determine_target_branch":
                state = await _execute(resolve_target, state, request)
                step = triage.next_after_triage_target(state)

            if step == "check_cve_applicability":
                prepared = None
                try:
                    state, prepared, step = await _execute(prepare_applicability, state, override)
                    if prepared is not None:
                        app_context = replace(context, working_directory=str(prepared.local_clone))
                        try:
                            applicability = await applicability_agent.run(
                                prepared.prompt, deps=app_context, **options
                            )
                            step = triage.apply_cve_applicability(state, applicability.output)
                        except (AgentRunError, ApplicationError, ActivityError, ValueError) as error:
                            workflow.logger.warning(
                                "Applicability check failed for %s: %s", state.jira_issue, error
                            )
                            state.applicability_check_skipped = True
                            step = triage.next_after_cve_applicability(state)
                finally:
                    await _execute(cleanup_applicability, prepared.builddir if prepared else None)
                    state.applicability_local_clone = None
                    state.applicability_unpacked_sources = None

            if step == "verify_rebuild_buildroot" and (not request.defer_rebuild_check or state.triage_only):
                triage.apply_rebuild_buildroot(state, await _execute(read_buildroot, state, override))

        follow_up = request.follow_up_requested and not (request.triage_only_requested or state.triage_only)
        if request.collect_jira_fields and request.triage_only_requested:
            state.triage_only = True
        return TriageAnalysisResult(state, follow_up, triage_usage, jira_fields)


def _durable_tool(schema_tool, *, gateway: bool) -> Tool:
    name = schema_tool.name
    tool_type = type(schema_tool)

    async def call(ctx: RunContext[TriageRunContext], **arguments: Any) -> Any:
        deps = ctx.deps
        if gateway:
            async with _gateway(deps.jira_issue, deps.rhel_config_override) as tools:
                return await invoke_pydantic_tool(name, available_tools=tools, **arguments)

        options = {}
        if env := get_mock_local_tool_env(deps.jira_issue):
            options["env"] = env
        if deps.working_directory:
            options["working_directory"] = Path(deps.working_directory)
        with rhel_config_override(deps.rhel_config_override):
            return await invoke_pydantic_tool(tool_type(options=options), **arguments)

    return Tool.from_schema(
        call,
        name=name,
        description=schema_tool.description,
        json_schema=schema_tool.input_schema.model_json_schema(),
        takes_ctx=True,
        sequential=True,
    )


async def _propose_jira_fields(**arguments: Any) -> dict:
    try:
        proposal = SetJiraFieldsToolInput.model_validate(arguments)
    except ValidationError as error:
        raise ModelRetry(str(error)) from error
    return proposal.model_dump(mode="json", exclude_none=True)


async def create_analysis_worker(
    client: Client,
    task_queue: str,
    model: Model,
    *,
    publication: bool = False,
    gateway_connection_attempts: int = 1,
    ingress: bool = False,
) -> Worker:
    global triage_model_agent, applicability_agent
    async with mcp_tools(
        os.environ["MCP_GATEWAY_URL"], max_retries=gateway_connection_attempts
    ) as gateway_tools:
        triage_tools = [
            _durable_tool(tool, gateway=True)
            for tool in gateway_tools
            if tool.name in triage.TRIAGE_GATEWAY_TOOLS and tool.name != "set_jira_fields"
        ]
        applicability_tools = [
            _durable_tool(tool, gateway=True)
            for tool in gateway_tools
            if tool.name in APPLICABILITY_GATEWAY_TOOLS
        ]
        if publication:
            triage_tools.append(
                Tool.from_schema(
                    _propose_jira_fields,
                    name="set_jira_fields",
                    description="Propose values for empty Jira fields to apply after triage.",
                    json_schema=SetJiraFieldsToolInput.model_json_schema(),
                    sequential=True,
                )
            )

    def durability() -> TemporalDurability:
        return TemporalDurability(
            activity_config={
                "start_to_close_timeout": timedelta(minutes=10),
                "retry_policy": RETRY_POLICY,
            }
        )

    triage_model_agent = triage.create_triage_agent(
        [],
        model=model,
        publish_jira_fields=publication,
        capabilities=[durability()],
        tools=[
            _durable_tool(RunShellCommandTool(), gateway=False),
            _durable_tool(VersionMapperTool(), gateway=False),
            *triage_tools,
        ],
    )
    applicability_agent = create_triage_applicability_agent(
        [],
        {},
        model=model,
        deps_type=TriageRunContext,
        capabilities=[durability()],
        tools=[
            _durable_tool(ViewTool(), gateway=False),
            _durable_tool(SearchTextTool(), gateway=False),
            _durable_tool(RunShellCommandTool(), gateway=False),
            *applicability_tools,
        ],
    )
    workflows = [TriageAnalysisWorkflow]
    activities = [
        read_eligibility,
        prepare_analysis,
        normalize_version,
        read_rebase_author,
        resolve_target,
        prepare_applicability,
        cleanup_applicability,
        read_buildroot,
    ]
    if publication:
        from ymir.agents.temporal_triage_publication import PUBLICATION_ACTIVITIES

        activities.append(prepare_publication_analysis)
        activities.extend(PUBLICATION_ACTIVITIES)

    if ingress:
        from ymir.agents.temporal_package_coordination import (
            PackageStreamActivities,
            PackageStreamWorkflow,
            plan_package_stream,
            release_coordinated_triage,
        )
        from ymir.agents.temporal_triage_ingress import INGRESS_ACTIVITIES, TriageIngressWorkflow
        from ymir.agents.temporal_triage_publication import TriageRequestReceiptWorkflow

        coordination = PackageStreamActivities(client, task_queue)
        workflows.extend((TriageIngressWorkflow, TriageRequestReceiptWorkflow, PackageStreamWorkflow))
        activities.extend(
            [
                *INGRESS_ACTIVITIES,
                plan_package_stream,
                release_coordinated_triage,
                coordination.submit_package_coordination,
                coordination.submit_sibling_triage,
            ]
        )
    return Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=list(dict.fromkeys(activities)),
        plugins=[AgentPlugin(triage_model_agent), AgentPlugin(applicability_agent)],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                *(definition.__module__ for definition in workflows)
            )
        ),
    )
