"""Execute and publish a triage request admitted by the issue workflow."""

import json
import os
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import partial
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from ymir.agents import tasks
from ymir.agents import triage_agent as triage
from ymir.agents.temporal_triage_analysis import (
    TriageAnalysisRequest,
    TriageAnalysisResult,
    TriageAnalysisWorkflow,
    _gateway,
)
from ymir.agents.temporal_triage_provisioning import TriagePodActivities
from ymir.agents.temporal_worker_pods import WorkerPod, run_in_worker_pod
from ymir.agents.triage_agent import TriageState
from ymir.common.base_utils import fix_await, redis_client, resolve_dist_git_namespace
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import (
    POSTPONED_RESOLUTIONS,
    BackportData,
    IssueStatus,
    RebaseData,
    RebuildData,
    Resolution,
    Task,
)
from ymir.common.temporal_triage import triage_issue_workflow_id
from ymir.common.utils import run_tool
from ymir.common.version_utils import detect_modular_issue, parse_rhel_version
from ymir.tools.privileged.jira import SetJiraFieldsToolInput

RECEIPT_TTL = int(timedelta(days=7).total_seconds())
WRITE_RETRY = RetryPolicy(maximum_attempts=8, maximum_interval=timedelta(seconds=60))
FIX_RESOLUTIONS = (Resolution.BACKPORT, Resolution.REBASE, Resolution.REBUILD)
REQUEST_ALREADY_STARTED = "TriageRequestAlreadyStarted"
POD_CLEANUP_FAILED = "TriagePodCleanupFailed"

# A consumer can pop an item before the client gets the response. Check the
# receipt instead of inspecting the queue. Both keys live on Ymir's Valkey.
ENQUEUE_WITH_RECEIPT = """
local receipt = redis.call('GET', KEYS[2])
if receipt then
    if receipt == ARGV[1] then return 0 end
    return -1
end
redis.call('LPUSH', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[3])
return 1
"""


@dataclass
class TriagePublicationRequest:
    analysis: TriageAnalysisRequest
    request_id: str
    dry_run: bool = True
    enqueue_reproducer: bool = False
    provision_pod: bool = False
    # Preserve the mode recorded in older Temporal activity results for replay.
    coordinate: bool = True

    def operation_id(self, step: str) -> str:
        identity = ["ymir-triage-v1", self.analysis.jira_issue.upper(), self.request_id, step]
        return sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()

    def workflow_id(self) -> str:
        return triage_issue_workflow_id(self.analysis.jira_issue)

    def receipt_workflow_id(self) -> str:
        mode = "preview" if self.dry_run else "publish"
        return f"triage-request/{self.analysis.jira_issue.upper()}/{mode}/{self.operation_id('request')}"


@dataclass
class PublicationInputs:
    labels: list[str]
    status: str
    dry_run: bool


@dataclass
class RedisHandoff:
    destination: str
    operation_id: str
    payload: str
    purpose: str

    @property
    def receipt_key(self) -> str:
        return f"ymir:temporal-triage:handoff:{self.operation_id}"


@dataclass
class TriagePublicationResult:
    analysis: TriageAnalysisResult | None
    destinations: list[str]
    dry_run: bool
    skipped_reason: str | None = None
    coordinated_handoffs: list[RedisHandoff] | None = None


def stream_rank(version: str) -> tuple[int, int] | None:
    stream = parse_rhel_version(version)
    return (int(stream[0]), int(stream[1])) if stream else None


@dataclass(frozen=True)
class PackageStream:
    package: str
    component: str
    version: str
    namespace: str
    branch: str

    @property
    def workflow_id(self) -> str:
        identity = [self.namespace, self.package, self.component, self.version, self.branch]
        digest = sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]
        return f"package-stream/{self.package}/{self.branch}/{digest}"


def package_stream(state: TriageState) -> PackageStream | None:
    finding = state.triage_result
    if not finding or finding.resolution not in FIX_RESOLUTIONS or not state.target_branch:
        return None
    data = finding.data
    if not isinstance(data, (BackportData, RebaseData, RebuildData)):
        return None
    version = stream_rank(data.fix_version or "")
    if version is None:
        return None
    namespace = resolve_dist_git_namespace(state.target_branch, state.dist_git_namespace)
    return PackageStream(
        data.package.lower(),
        (state.raw_downstream_component or data.package).lower(),
        f"rhel-{version[0]}.{version[1]}",
        namespace,
        state.target_branch,
    )


@workflow.defn
class TriageRequestReceiptWorkflow:
    """Record which issue run owns a request, including after that run closes."""

    @workflow.run
    async def run(self, owner_run_id: str) -> str:
        return owner_run_id


def _writes_disabled() -> bool:
    return any(os.getenv(name, "false").lower() == "true" for name in ("DRY_RUN", "JIRA_DRY_RUN"))


def _require_writes() -> None:
    if _writes_disabled():
        raise ApplicationError("Publication worker has writes disabled", non_retryable=True)


async def _jira_fields(issue: str, tools) -> dict:
    details = await run_tool("get_jira_details", available_tools=tools, issue_key=issue)
    return details["fields"]


@activity.defn
async def read_publication_inputs(request: TriagePublicationRequest) -> PublicationInputs:
    async with _gateway(request.analysis.jira_issue, request.analysis.config_override) as tools:
        fields = await _jira_fields(request.analysis.jira_issue, tools)
    return PublicationInputs(
        labels=fields["labels"],
        status=fields["status"]["name"],
        dry_run=request.dry_run or _writes_disabled(),
    )


async def _post_comment(request: TriagePublicationRequest, step: str, body: str) -> None:
    _require_writes()
    issue = request.analysis.jira_issue
    operation_id = request.operation_id(step)
    async with _gateway(issue, request.analysis.config_override) as tools:

        async def receipt_exists() -> bool:
            result = await run_tool(
                "find_jira_comment_operation",
                available_tools=tools,
                issue_key=issue,
                operation_id=operation_id,
            )
            return result["exists"]

        if await receipt_exists():
            return

        # Jira's POST has no idempotency key. Once a worker starts the POST,
        # subsequent attempts only look for evidence of its outcome.
        async with redis_client(os.environ["REDIS_URL"], socket_timeout=10) as redis:
            claimed = await redis.set(
                f"ymir:temporal-triage:comment:{operation_id}", "posting", nx=True, ex=RECEIPT_TTL
            )
        if claimed:
            await tasks.comment_in_jira(issue, "Triage", f"Ymir operation: {operation_id}\n\n{body}", tools)
        if not await receipt_exists():
            raise ApplicationError(
                f"Jira comment outcome is uncertain for {issue}, operation {operation_id}",
                type="UncertainJiraComment",
            )


def _jira_record(record: dict) -> str:
    encoded = json.dumps({"schema": 1, **record}, sort_keys=True, ensure_ascii=False)
    return f"{{code:json}}\n{encoded}\n{{code}}"


@activity.defn
async def acknowledge_triage_request(request: TriagePublicationRequest, skipped_reason: str | None) -> None:
    mode = "triage-only" if request.analysis.triage_only_requested else "full"
    body = f"Accepted {mode} processing request."
    if skipped_reason:
        body = skipped_reason
    await _post_comment(
        request,
        "acknowledgement",
        body
        + "\n\n"
        + _jira_record(
            {
                "kind": "triage-request",
                "issue": request.analysis.jira_issue,
                "request_id": request.request_id,
                "mode": mode,
                "follow_up_requested": mode == "full",
                "skipped_reason": skipped_reason,
            }
        ),
    )


@activity.defn
async def apply_triage_jira_fields(request: TriagePublicationRequest, fields: SetJiraFieldsToolInput) -> None:
    _require_writes()
    async with _gateway(request.analysis.jira_issue, request.analysis.config_override) as tools:
        # The existing gateway setter rereads the issue and fills only empty
        # fields, so a retried update never overwrites an applied value.
        result = await run_tool(
            "set_jira_fields", available_tools=tools, **fields.model_dump(mode="json", exclude_none=True)
        )
        if isinstance(result, str) and result.startswith(("Dry run", "JIRA_DRY_RUN")):
            raise ApplicationError(
                "Jira gateway skipped the field update in dry-run mode", non_retryable=True
            )


@activity.defn
async def publish_triage_finding(
    request: TriagePublicationRequest, result: TriageAnalysisResult, handoffs: list[RedisHandoff]
) -> None:
    state = result.state
    output = state.triage_result
    body = output.format_for_comment(
        auto_chain=result.follow_up_requested,
        triage_only=triage._defers_fix(output.resolution, state.triage_only),
    )
    if coordination_workflow_id := result.coordination_workflow_id:
        body += f"\n\nFollow-up is waiting on package-stream coordination ({coordination_workflow_id})."
    if state.applicability_check_skipped:
        body += "\n\n_Note: The CVE applicability check could not be performed._"
    if state.hold_for_manual_branch_creation:
        body += "\n\n" + tasks.manual_branch_creation_comment(
            output.data.package, state.target_branch, state.jira_issue
        )

    record = _jira_record(
        {
            "kind": "triage-finding",
            "issue": state.jira_issue,
            "request_id": request.request_id,
            "target_branch": state.target_branch,
            "dist_git_namespace": state.dist_git_namespace,
            "triage_only": state.triage_only,
            "follow_up_requested": result.follow_up_requested,
            "manual_branch_needed": state.hold_for_manual_branch_creation,
            "applicability_check_skipped": state.applicability_check_skipped,
            "finding": output.model_dump(mode="json", exclude={"data": {"triage_summary"}}),
            "planned_follow_up": [item.purpose for item in handoffs],
            **(
                {
                    "coordination_workflow_id": coordination_workflow_id,
                    "user_triggered": request.analysis.user_triggered,
                    "force_cve_triage": request.analysis.force_cve_triage,
                }
                if coordination_workflow_id
                else {}
            ),
        }
    )
    await _post_comment(request, "finding", f"{body}\n\n{record}")


@activity.defn
async def apply_triage_labels(request: TriagePublicationRequest, add: list[str], remove: list[str]) -> None:
    _require_writes()
    issue = request.analysis.jira_issue
    async with _gateway(issue, request.analysis.config_override) as tools:
        current = set((await _jira_fields(issue, tools))["labels"])
        to_add, to_remove = sorted(set(add) - current), sorted(set(remove) & current)
        if to_add or to_remove:
            result = await run_tool(
                "edit_jira_labels",
                available_tools=tools,
                issue_key=issue,
                labels_to_add=to_add,
                labels_to_remove=to_remove,
            )
            if isinstance(result, str) and result.startswith(("Dry run", "JIRA_DRY_RUN")):
                raise ApplicationError(
                    "Jira gateway skipped the label update in dry-run mode", non_retryable=True
                )


@activity.defn
async def reproducer_enabled(request: TriagePublicationRequest, package: str) -> bool:
    async with _gateway(request.analysis.jira_issue, request.analysis.config_override) as tools:
        try:
            config = await tasks.fetch_reproducer_config(package, tools)
        except tasks.InvalidReproducerConfigError as error:
            activity.logger.warning("Skipping reproducer for %s: %s", package, error)
            return False
    return config.enabled


async def _enqueue_with_receipt(handoff: RedisHandoff) -> int:
    fingerprint = sha256(handoff.payload.encode()).hexdigest()
    async with redis_client(os.environ["REDIS_URL"], socket_timeout=10) as redis:
        return await fix_await(
            redis.eval(
                ENQUEUE_WITH_RECEIPT,
                2,
                handoff.destination,
                handoff.receipt_key,
                fingerprint,
                handoff.payload,
                RECEIPT_TTL,
            )
        )


@activity.defn
async def enqueue_triage_handoff(handoff: RedisHandoff) -> None:
    _require_writes()
    if await _enqueue_with_receipt(handoff) == -1:
        raise ApplicationError(
            f"Redis handoff {handoff.operation_id} already has a different payload", non_retryable=True
        )


def _task_handoff(
    request: TriagePublicationRequest, queue: str, metadata: dict, purpose: str
) -> RedisHandoff:
    operation_id = request.operation_id(f"handoff:{queue}")
    task = Task(
        execution_id=uuid5(NAMESPACE_URL, operation_id),
        metadata=metadata,
        user_triggered=request.analysis.user_triggered,
    )
    # Sort nested keys so the same typed finding has the same Redis fingerprint
    # when serialized again on a replacement worker.
    payload = json.dumps(task.model_dump(mode="json"), sort_keys=True)
    return RedisHandoff(queue, operation_id, payload, purpose)


def _downstream_handoff(
    request: TriagePublicationRequest, result: TriageAnalysisResult, labels: list[str]
) -> RedisHandoff | None:
    state = result.state
    output = state.triage_result
    resolution = output.resolution
    if resolution in POSTPONED_RESOLUTIONS or (
        resolution == Resolution.OPEN_ENDED_ANALYSIS and result.follow_up_requested
    ):
        destination = (
            RedisQueues.POSTPONED_LIST
            if resolution in POSTPONED_RESOLUTIONS
            else RedisQueues.OPEN_ENDED_ANALYSIS_LIST
        ).value
        return RedisHandoff(
            destination,
            request.operation_id(f"handoff:{destination}"),
            output.data.model_dump_json(),
            resolution.value,
        )

    if not result.follow_up_requested:
        return None
    if resolution == Resolution.CLARIFICATION_NEEDED:
        queue = RedisQueues.CLARIFICATION_NEEDED_QUEUE.value
    elif state.target_branch and not state.hold_for_manual_branch_creation and resolution in FIX_RESOLUTIONS:
        if resolution == Resolution.REBASE and (
            state.is_rebase_sibling
            or JiraLabels.REBASE_SIBLING.value in labels
            or state.rebase_waiting_for_siblings
            or JiraLabels.WAITING_FOR_SIBLINGS.value in labels
        ):
            return None
        queue_for_branch = {
            Resolution.BACKPORT: RedisQueues.get_backport_queue_for_branch,
            Resolution.REBASE: RedisQueues.get_rebase_queue_for_branch,
            Resolution.REBUILD: RedisQueues.get_rebuild_queue_for_branch,
        }[resolution]
        queue = queue_for_branch(state.target_branch, request.analysis.user_triggered)
    else:
        return None
    return _task_handoff(request, queue, state.model_dump(mode="json"), resolution.value)


def _terminal_labels(result: TriageAnalysisResult, labels: list[str]) -> tuple[list[str], list[str]]:
    state = result.state
    output = state.triage_result
    remove = {
        JiraLabels.TRIAGE_IN_PROGRESS.value,
        JiraLabels.MANUAL_BRANCH_NEEDED.value,
        *(label.value for label in triage._RESOLUTION_TO_LABEL.values()),
        *JiraLabels.deferred_triage_labels(),
        *(label.value for label in triage._CONFIDENCE_TO_LABEL.values()),
    }
    add = set()
    waiting = state.rebase_waiting_for_siblings or JiraLabels.WAITING_FOR_SIBLINGS.value in labels
    if state.hold_for_manual_branch_creation:
        add.add(JiraLabels.MANUAL_BRANCH_NEEDED.value)
    elif not triage._should_defer_terminal_label(output.resolution, state.triage_only, waiting) and (
        label := triage._resolution_label(output.resolution, state.triage_only)
    ):
        add.add(label.value)
    if output.confidence and (label := triage._CONFIDENCE_TO_LABEL.get(output.confidence.level)):
        add.add(label.value)
    return sorted(add), sorted(remove - add)


async def _run_activity(fn, *args, operation_id: str | None = None, task_queue: str | None = None):
    return await workflow.execute_activity(
        fn,
        args=args,
        activity_id=operation_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(minutes=5),
        schedule_to_close_timeout=timedelta(minutes=15),
        retry_policy=WRITE_RETRY,
    )


async def claim_triage_request(request: TriagePublicationRequest) -> None:
    try:
        await workflow.execute_child_workflow(
            TriageRequestReceiptWorkflow.run,
            workflow.info().first_execution_run_id,
            id=request.receipt_workflow_id(),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError as error:
        raise ApplicationError(
            "Triage request already has an owning run", type=REQUEST_ALREADY_STARTED, non_retryable=True
        ) from error


async def execute_triage_request(
    request: TriagePublicationRequest, execution_id: str
) -> TriagePublicationResult:
    if not request.provision_pod:
        return await _run_request(request, execution_id)

    pod = WorkerPod(
        f"ymir-triage-{execution_id}",
        request.analysis.jira_issue,
        request.dry_run,
    )
    return await run_in_worker_pod(
        pod,
        lambda queue: _run_request(request, execution_id, task_queue=queue),
        activities=TriagePodActivities,
        run_timeout=timedelta(minutes=43),
        cleanup_retry_policy=WRITE_RETRY,
        cleanup_error_type=POD_CLEANUP_FAILED,
    )


async def _run_request(
    request: TriagePublicationRequest, execution_id: str, *, task_queue: str | None = None
) -> TriagePublicationResult:
    info = workflow.info()
    execute = partial(_run_activity, task_queue=task_queue)
    analysis = request.analysis
    inputs = await execute(read_publication_inputs, request)
    skipped = None
    if inputs.status in (IssueStatus.CLOSED.value, IssueStatus.DONE.value):
        skipped = f"Issue is already {inputs.status}. Skipping processing."
    if skipped:
        return TriagePublicationResult(None, [], inputs.dry_run, skipped)

    result = await workflow.execute_child_workflow(
        TriageAnalysisWorkflow.run,
        replace(
            analysis,
            defer_rebuild_check=request.coordinate
            and not inputs.dry_run
            and not analysis.triage_only_requested
            and (analysis.user_triggered or analysis.follow_up_requested)
            and (analysis.user_triggered or JiraLabels.TRIAGE_ONLY.value not in inputs.labels),
            follow_up_requested=not analysis.triage_only_requested
            if analysis.user_triggered
            else analysis.follow_up_requested,
            triage_only_requested=analysis.triage_only_requested
            or (not analysis.user_triggered and JiraLabels.TRIAGE_ONLY.value in inputs.labels),
        ),
        id=f"{info.workflow_id}/analysis/{execution_id}",
        task_queue=task_queue,
        cancellation_type=workflow.ChildWorkflowCancellationType.TRY_CANCEL,
    )
    state = result.state
    if state.triage_result is None:
        raise ApplicationError(f"Triage returned no finding for {state.jira_issue}", non_retryable=True)
    if state.triage_result.resolution == Resolution.ERROR:
        raise ApplicationError(
            f"Triage returned an error finding for {state.jira_issue}",
            type="TriageErrorFinding",
            non_retryable=True,
        )

    handoffs = []
    if handoff := _downstream_handoff(request, result, inputs.labels):
        handoffs.append(handoff)
    if (
        request.enqueue_reproducer
        and not state.triage_only
        and state.triage_result.resolution in triage._REPRODUCER_ELIGIBLE_RESOLUTIONS
        and not detect_modular_issue(
            jira_summary=state.jira_summary,
            raw_downstream_component=state.raw_downstream_component,
            downstream_component=state.downstream_component,
        )
        and (reproducer := triage._build_reproducer_input(state))
        and await execute(reproducer_enabled, request, reproducer.package)
    ):
        handoffs.append(
            _task_handoff(
                request,
                RedisQueues.get_reproducer_queue(analysis.user_triggered),
                reproducer.model_dump(mode="json"),
                "reproducer",
            )
        )

    held = bool(
        request.coordinate
        and not inputs.dry_run
        and any(item.purpose == state.triage_result.resolution.value for item in handoffs)
        and state.triage_result.resolution in FIX_RESOLUTIONS
    )

    if not inputs.dry_run:
        if state.triage_result.resolution in FIX_RESOLUTIONS:
            seen = set()
            for proposal in result.jira_fields:
                key = json.dumps(proposal.model_dump(mode="json", exclude_none=True), sort_keys=True)
                if key not in seen:
                    await execute(
                        apply_triage_jira_fields,
                        request,
                        proposal,
                        operation_id=request.operation_id(f"fields:{key}"),
                    )
                    seen.add(key)
        if held:
            if (key := package_stream(state)) is None:
                raise ApplicationError("Cannot identify the Jira package-stream", non_retryable=True)
            result = replace(result, coordination_workflow_id=key.workflow_id)
        await execute(
            publish_triage_finding,
            request,
            result,
            handoffs,
            operation_id=request.operation_id("finding"),
        )
        add, remove = _terminal_labels(result, inputs.labels)
        if request.coordinate:
            waiting = JiraLabels.TRIAGE_COORDINATION_WAITING.value
            if held:
                label = triage._resolution_label(state.triage_result.resolution, state.triage_only)
                if label is not None:
                    remove.append(label.value)
                    add = [value for value in add if value != label.value]
                add.append(waiting)
            else:
                remove.append(waiting)
        await execute(apply_triage_labels, request, add, remove, operation_id=request.operation_id("labels"))
        if not held:
            for item in handoffs:
                await execute(enqueue_triage_handoff, item, operation_id=item.operation_id)

    destinations = [] if held else [item.destination for item in handoffs]
    return TriagePublicationResult(
        result, destinations, inputs.dry_run, coordinated_handoffs=handoffs if held else None
    )


PUBLICATION_ACTIVITIES = [
    read_publication_inputs,
    acknowledge_triage_request,
    apply_triage_jira_fields,
    publish_triage_finding,
    apply_triage_labels,
    reproducer_enabled,
    enqueue_triage_handoff,
]
