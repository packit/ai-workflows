"""Serialize Jira, CLI, and retry submissions under one issue workflow."""

import asyncio
import os
from collections import deque
from dataclasses import dataclass
from hashlib import sha256

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError, FailureError

from ymir.agents.pydantic_agent import pydantic_model_settings
from ymir.agents.temporal_package_coordination import (
    CoordinatedTriage,
    PackageStreamActivities,
)
from ymir.agents.temporal_triage_analysis import TriageAnalysisRequest
from ymir.agents.temporal_triage_publication import (
    POD_CLEANUP_FAILED,
    REQUEST_ALREADY_STARTED,
    TriagePublicationRequest,
    TriagePublicationResult,
    _jira_record,
    _post_comment,
    _run_activity,
    acknowledge_triage_request,
    apply_triage_labels,
    claim_triage_request,
    execute_triage_request,
    package_stream,
    read_publication_inputs,
)
from ymir.agents.utils import get_agent_execution_config, resolve_chat_model_override
from ymir.common.constants import JiraLabels
from ymir.common.models import IssueStatus
from ymir.common.temporal_triage import TriageIntent, triage_issue_workflow_id
from ymir.jira_issue_fetcher.jira_issue_fetcher import JiraIssueFetcher


@activity.defn
async def prepare_triage_intent(intent: TriageIntent) -> TriagePublicationRequest:
    if intent.config_override is not None and not (
        intent.dry_run and os.getenv("ALLOW_RHEL_CONFIG_OVERRIDE", "").lower() == "true"
    ):
        raise ApplicationError("Triage config overrides require a dry-run test worker", non_retryable=True)
    resolve_chat_model_override("triage")
    return TriagePublicationRequest(
        TriageAnalysisRequest(
            jira_issue=intent.issue.strip().upper(),
            model_settings=dict(pydantic_model_settings(os.environ["CHAT_MODEL"])),
            request_limit=get_agent_execution_config()["max_iterations"],
            force_cve_triage=intent.force_cve_triage,
            config_override=intent.config_override,
            user_triggered=intent.user_triggered,
            triage_only_requested=intent.triage_only,
            follow_up_requested=not intent.triage_only and os.getenv("AUTO_CHAIN", "true").lower() == "true",
        ),
        intent.request_id,
        dry_run=intent.dry_run,
        enqueue_reproducer=os.getenv("TRIAGE_ENQUEUE_REPRODUCER", "true").lower() == "true",
        provision_pod=os.getenv("TEMPORAL_TRIAGE_INLINE", "").lower() != "true",
    )


@activity.defn
async def check_triage_intent(request: TriagePublicationRequest, intent: TriageIntent) -> str | None:
    inputs = await read_publication_inputs(request)
    if inputs.dry_run != request.dry_run:
        raise ApplicationError("Triage controller has Jira writes disabled", non_retryable=True)
    if inputs.status in {IssueStatus.CLOSED.value, IssueStatus.DONE.value}:
        return f"Issue is already {inputs.status}. Skipping processing."
    if intent.source == "discovery" and any(label.startswith("ymir_") for label in inputs.labels):
        return "Issue has entered a Ymir workflow since discovery."
    if intent.retry_label and intent.retry_label not in inputs.labels:
        return "Issue no longer has the retry or stale work label."
    return None


@activity.defn
async def begin_triage_intent(request: TriagePublicationRequest, intent: TriageIntent, skipped: bool) -> None:
    remove = []
    if intent.triggers:
        labels = (await read_publication_inputs(request)).labels
        fetcher = JiraIssueFetcher()
        for label, event in intent.triggers.items():
            if (
                label in labels
                and await asyncio.to_thread(fetcher.authorized_trigger_event, intent.issue, label) == event
            ):
                remove.append(label)
    if intent.retry_label:
        if intent.retry_comment and not skipped:
            await _post_comment(request, "retry-reason", intent.retry_comment)
        remove.extend([intent.retry_label, JiraLabels.RETRY_NEEDED.value])
    add = [] if skipped else [JiraLabels.TRIAGE_IN_PROGRESS.value]
    if intent.user_triggered and not skipped and intent.source != "cli":
        marker = JiraLabels.TRIAGE_ONLY.value
        (add if intent.triage_only else remove).append(marker)
    await apply_triage_labels(request, add, [label for label in remove if label not in add])


@activity.defn
async def record_triage_failure(request: TriagePublicationRequest, run_id: str) -> None:
    if request.dry_run:
        return
    workflow_id = request.workflow_id()
    await _post_comment(
        request,
        "failed",
        f"Triage stopped after a terminal failure. See Temporal workflow {workflow_id}, run {run_id}. "
        "Review any finding and follow-up already recorded before retrying.\n\n"
        + _jira_record(
            {
                "kind": "triage-failure",
                "issue": request.analysis.jira_issue,
                "request_id": request.request_id,
                "workflow_id": workflow_id,
                "run_id": run_id,
            }
        ),
    )
    await apply_triage_labels(
        request, [JiraLabels.TRIAGE_ERRORED.value], [JiraLabels.TRIAGE_IN_PROGRESS.value]
    )


@dataclass
class TriageIntentOutcome:
    request_id: str
    status: str
    detail: str | None = None
    preview: TriagePublicationResult | None = None


@workflow.defn
class TriageIngressWorkflow:
    @workflow.init
    def __init__(self, issue: str):
        self.issue = issue
        self.pending: deque[TriageIntent] = deque()
        self.waiting_for_result: set[str] = set()
        self.request_results: dict[str, dict] = {}
        self.pod_cleanup_failed = False

    @workflow.update
    def enqueue(self, intent: TriageIntent) -> str:
        if intent.source == "discovery" and self.pending:
            return "coalesced"
        self.pending.append(intent)
        return "accepted"

    @workflow.update
    async def enqueue_and_wait(self, intent: TriageIntent) -> dict:
        self.waiting_for_result.add(intent.update_id)
        self.pending.append(intent)
        await workflow.wait_condition(lambda: intent.update_id in self.request_results)
        return self.request_results.pop(intent.update_id)

    @enqueue_and_wait.validator
    def validate_waiting_intent(self, intent: TriageIntent) -> None:
        self.validate_intent(intent)

    def _complete_request(self, intent: TriageIntent, status: str, detail=None, result=None) -> None:
        if intent.update_id in self.waiting_for_result:
            self.waiting_for_result.remove(intent.update_id)
            output = result.analysis.state.triage_result if result and result.analysis else None
            self.request_results[intent.update_id] = {
                "status": status,
                "detail": detail,
                "output": output.model_dump(mode="json") if output else None,
            }

    @enqueue.validator
    def validate_intent(self, intent: TriageIntent) -> None:
        if self.pod_cleanup_failed:
            raise ApplicationError("Triage is closing after a pod cleanup failure", non_retryable=True)
        if triage_issue_workflow_id(intent.issue) != triage_issue_workflow_id(self.issue):
            raise ApplicationError("Triage intent belongs to a different issue", non_retryable=True)
        if not intent.request_id.strip() or len(intent.request_id) > 200:
            raise ApplicationError("Expected a request ID of 1 to 200 characters", non_retryable=True)
        if intent.source not in {"cli", "jira", "discovery", "retry"}:
            raise ApplicationError("Unknown triage submission source", non_retryable=True)

    @workflow.run
    async def run(self, issue: str) -> list[TriageIntentOutcome]:
        info = workflow.info()
        if info.workflow_id != triage_issue_workflow_id(issue):
            raise ApplicationError("Start triage ingress with the Jira issue Workflow ID", non_retryable=True)
        await workflow.wait_condition(lambda: bool(self.pending))
        outcomes = []
        failed = []
        last_failure = None
        while True:
            if not self.pending:
                await workflow.wait_condition(lambda: bool(self.pending) or workflow.all_handlers_finished())
                if not self.pending:
                    break
            intent = self.pending[0]
            request = None
            result = None
            try:
                request = await _run_activity(prepare_triage_intent, intent)
                await claim_triage_request(request)
                skipped = await _run_activity(check_triage_intent, request, intent)
                if not request.dry_run:
                    if intent.user_triggered:
                        await _run_activity(
                            acknowledge_triage_request,
                            request,
                            skipped,
                            operation_id=request.operation_id("acknowledgement"),
                        )
                    await _run_activity(begin_triage_intent, request, intent, skipped is not None)
                if skipped is None:
                    pod_identity = f"{info.first_execution_run_id}:{intent.update_id}"
                    pod_id = sha256(pod_identity.encode()).hexdigest()[:36]
                    result = await execute_triage_request(request, pod_id)
                    skipped = result.skipped_reason
                    if result.coordinated_handoffs:
                        state = result.analysis.state
                        if (key := package_stream(state)) is None:
                            raise ApplicationError(
                                "Cannot identify the Jira package-stream", non_retryable=True
                            )
                        await _run_activity(
                            PackageStreamActivities.submit_package_coordination,
                            CoordinatedTriage(key, request, state, result.coordinated_handoffs),
                            operation_id=request.operation_id("package-stream"),
                        )
                self._complete_request(intent, "skipped" if skipped else "completed", skipped, result)
                outcomes.append(
                    TriageIntentOutcome(
                        intent.request_id,
                        "skipped" if skipped else "completed",
                        skipped,
                        result if intent.dry_run else None,
                    )
                )
            except FailureError as error:
                if isinstance(error, ApplicationError) and error.type == REQUEST_ALREADY_STARTED:
                    outcomes.append(TriageIntentOutcome(intent.request_id, "duplicate"))
                    self._complete_request(intent, "duplicate")
                else:
                    last_failure = error
                    failed.append(intent.request_id)
                    self._complete_request(intent, "failed", str(error))
                    if not intent.dry_run:
                        request = request or TriagePublicationRequest(
                            TriageAnalysisRequest(self.issue, {}, 0), intent.request_id, dry_run=False
                        )
                        try:
                            await _run_activity(
                                record_triage_failure,
                                request,
                                info.run_id,
                                operation_id=request.operation_id("failed"),
                            )
                        except FailureError:
                            workflow.logger.exception("Could not record terminal triage failure in Jira")
                if isinstance(error, ApplicationError) and error.type == POD_CLEANUP_FAILED:
                    self.pod_cleanup_failed = True
                    self.pending.popleft()
                    outcomes.extend(
                        TriageIntentOutcome(item.request_id, "unstarted") for item in self.pending
                    )
                    for item in self.pending:
                        self._complete_request(item, "unstarted", "An earlier pod could not be cleaned up.")
                    break
            self.pending.popleft()

        await workflow.wait_condition(workflow.all_handlers_finished)
        if failed:
            raise ApplicationError(
                "One or more triage requests failed",
                failed,
                outcomes,
                type="TriageRequestsFailed",
                non_retryable=True,
            ) from last_failure
        return outcomes


INGRESS_ACTIVITIES = [
    prepare_triage_intent,
    check_triage_intent,
    acknowledge_triage_request,
    begin_triage_intent,
    record_triage_failure,
]
