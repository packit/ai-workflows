"""Submission contract shared by triage producers and the Temporal worker."""

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

CONTROL_QUEUE = "ymir-triage-control"
INGRESS_WORKFLOW = "TriageIngressWorkflow"


def triage_issue_workflow_id(issue: str) -> str:
    return f"triage/{issue.strip().upper()}"


def temporal_triage_accepts_requests() -> bool:
    return os.getenv("TEMPORAL_TRIAGE_ADMISSIONS", "true").lower() == "true"


class TriageAdmissionsPaused(RuntimeError):
    pass


@dataclass
class TriageIntent:
    issue: str
    request_id: str
    source: str
    dry_run: bool = True
    user_triggered: bool = False
    triage_only: bool = False
    force_cve_triage: bool = False
    triggers: dict[str, str] = field(default_factory=dict)
    retry_label: str | None = None
    retry_comment: str | None = None
    config_override: dict[str, Any] | None = None

    @property
    def update_id(self) -> str:
        return f"{'preview' if self.dry_run else 'publish'}:{self.request_id}"


async def connect_triage_ingress():
    from temporalio.client import Client

    address = os.environ["TEMPORAL_ADDRESS"]
    return await Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))


async def submit_triage_intent(
    client, intent: TriageIntent, *, task_queue: str | None = None, wait: bool = False
) -> str | dict:
    """Durably enqueue an intent and optionally wait for that request's result."""
    from temporalio.client import WithStartWorkflowOperation, WorkflowUpdateStage
    from temporalio.common import WorkflowIDConflictPolicy

    if not temporal_triage_accepts_requests():
        raise TriageAdmissionsPaused("Temporal triage admissions are paused")
    returns_result = wait or intent.source == "cli"
    handle = await client.start_update_with_start_workflow(
        "enqueue_and_wait" if returns_result else "enqueue",
        intent,
        id=intent.update_id,
        start_workflow_operation=WithStartWorkflowOperation(
            INGRESS_WORKFLOW,
            intent.issue.strip().upper(),
            id=triage_issue_workflow_id(intent.issue),
            task_queue=task_queue or os.getenv("TEMPORAL_TRIAGE_CONTROL_QUEUE", CONTROL_QUEUE),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        ),
        wait_for_stage=WorkflowUpdateStage.ACCEPTED if returns_result else WorkflowUpdateStage.COMPLETED,
        result_type=dict if returns_result else str,
        rpc_timeout=timedelta(seconds=20),
    )
    return "accepted" if returns_result and not wait else await handle.result()
