"""Submission contract shared by triage producers and the Temporal worker."""

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

try:
    from temporalio import client as temporal_client
    from temporalio import common as temporal_common
except ModuleNotFoundError as error:
    if error.name != "temporalio":
        raise
    # The local Compose CLI can run without the Temporal SDK on the host.
    temporal_client = temporal_common = None

CONTROL_QUEUE = "ymir-triage-control"
INGRESS_WORKFLOW = "TriageIngressWorkflow"


def triage_issue_workflow_id(issue: str) -> str:
    return f"triage/{issue.strip().upper()}"


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
    if temporal_client is None:
        raise ModuleNotFoundError("The Temporal client requires temporalio", name="temporalio")
    address = os.environ["TEMPORAL_ADDRESS"]
    return await temporal_client.Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))


async def submit_triage_intent(
    client, intent: TriageIntent, *, task_queue: str | None = None, wait: bool = False
) -> str | dict:
    """Durably enqueue an intent and optionally wait for that request's result."""
    if temporal_client is None:
        raise ModuleNotFoundError("The Temporal client requires temporalio", name="temporalio")
    returns_result = wait or intent.source == "cli"
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
    return "accepted" if returns_result and not wait else await handle.result()
