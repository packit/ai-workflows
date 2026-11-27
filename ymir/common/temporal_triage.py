"""Submission contract shared by triage producers and the Temporal worker."""

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import client as temporal_client
from temporalio import common as temporal_common

CONTROL_QUEUE = "ymir-triage-control"
INGRESS_WORKFLOW = "TriageIngressWorkflow"


def triage_issue_workflow_id(issue: str) -> str:
    return f"triage/{issue.strip().upper()}"


@dataclass
class TriageIntent:
    issue: str
    request_id: str
    triggered_by: str
    dry_run: bool = True
    triage_only: bool = False
    force_cve_triage: bool = False
    triggers: dict[str, str] = field(default_factory=dict)
    retry_label: str | None = None
    retry_comment: str | None = None
    config_override: dict[str, Any] | None = None

    @property
    def is_user_triggered(self) -> bool:
        return self.triggered_by in {"user", "user_retry"}

    @property
    def update_id(self) -> str:
        return f"{'preview' if self.dry_run else 'publish'}:{self.request_id}"


async def connect_triage_ingress():
    address = os.environ["TEMPORAL_ADDRESS"]
    return await temporal_client.Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))


async def submit_triage_intent(
    client,
    intent: TriageIntent,
    *,
    task_queue: str | None = None,
    wait: bool = False,
    retain_result: bool = False,
) -> str | dict:
    """Submit an intent and await triage when wait is true.

    Set retain_result to enable later retrieval for a detached submission.
    """
    returns_result = wait or retain_result
    handle = await client.start_update_with_start_workflow(
        "enqueue_and_wait" if returns_result else "enqueue",
        intent,
        id=intent.update_id,
        start_workflow_operation=temporal_client.WithStartWorkflowOperation(
            INGRESS_WORKFLOW,
            intent.issue.strip().upper(),
            id=triage_issue_workflow_id(intent.issue),
            task_queue=task_queue or os.getenv("TEMPORAL_TRIAGE_CONTROL_QUEUE", CONTROL_QUEUE),
            id_conflict_policy=temporal_common.WorkflowIDConflictPolicy.USE_EXISTING,
        ),
        wait_for_stage=(
            temporal_client.WorkflowUpdateStage.ACCEPTED
            if returns_result
            else temporal_client.WorkflowUpdateStage.COMPLETED
        ),
        result_type=dict if returns_result else str,
        rpc_timeout=timedelta(seconds=20),
    )
    return "accepted" if retain_result and not wait else await handle.result()
