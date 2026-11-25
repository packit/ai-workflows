"""Verify the control workflow owns startup failure and cancellation cleanup."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("temporalio")

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError

from ymir.agents.temporal_triage_analysis import TriageAnalysisRequest
from ymir.agents.temporal_triage_provisioning import (
    EXECUTION_LABEL,
    GATEWAY_URL,
    READY_FILE,
    OpenShiftTriageProvisioner,
    TriagePod,
    create_triage_control_worker,
)
from ymir.agents.temporal_triage_publication import TriagePublicationRequest, TriagePublicationWorkflow

ADDRESS = os.getenv("TEMPORAL_TEST_ADDRESS")


def test_openshift_pod_shares_only_one_execution_workspace():
    backend = OpenShiftTriageProvisioner(
        "testing", Path("openshift/templates/temporal-triage-pod.yml"), "temporal.testing:7233", "testing"
    )
    pod = TriagePod("ymir-triage-fixture", "RHEL-987654", True)
    manifest = backend.manifest(pod)
    assert manifest["metadata"]["labels"][EXECUTION_LABEL] == pod.name
    assert manifest["metadata"]["namespace"] == "testing"
    assert manifest["spec"]["automountServiceAccountToken"] is False
    assert {"name": "workspace", "emptyDir": {}} in manifest["spec"]["volumes"]

    worker, gateway = manifest["spec"]["containers"]
    assert worker["name"] == "worker" and gateway["name"] == "gateway"
    for container in worker, gateway:
        assert {"name": "workspace", "mountPath": "/git-repos"} in container["volumeMounts"]
        assert {"name": "DRY_RUN", "value": "true"} in container["env"]
    assert {"name": "MCP_GATEWAY_URL", "value": GATEWAY_URL} in worker["env"]
    assert {"name": "TEMPORAL_TASK_QUEUE", "value": pod.task_queue} in worker["env"]
    assert {"name": "MCP_BIND_HOST", "value": "127.0.0.1"} in gateway["env"]
    assert worker["readinessProbe"]["exec"]["command"][-1] == READY_FILE


@pytest.mark.asyncio
@pytest.mark.skipif(not ADDRESS, reason="Set TEMPORAL_TEST_ADDRESS to a local Temporal server")
@pytest.mark.parametrize("failure", ["startup", "cancel", "lost"])
async def test_provisioning_cleans_up_after_failure(failure):
    created = asyncio.Event()
    removed = []
    ready = False

    class Backend:
        async def create(self, pod):
            created.set()

        async def ready(self, pod):
            nonlocal ready
            if failure == "startup":
                raise ApplicationError("Injected pod startup failure", non_retryable=True)
            if failure == "cancel":
                return False
            if ready:
                raise ApplicationError("Injected pod loss", non_retryable=True)
            ready = True
            return ready

        async def delete(self, pod):
            removed.append(pod)

    client = await Client.connect(ADDRESS, plugins=[PydanticAIPlugin()])
    token = uuid4().hex[:12]
    queue = f"triage-control-test-{token}"
    request = TriagePublicationRequest(
        TriageAnalysisRequest(f"RHEL-{int(token, 16)}", {}, 1), token, provision_pod=True
    )
    async with create_triage_control_worker(client, queue, Backend()):
        handle = await client.start_workflow(
            TriagePublicationWorkflow.run, request, id=request.workflow_id(), task_queue=queue
        )
        await asyncio.wait_for(created.wait(), timeout=15)
        if failure == "cancel":
            await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=20)
        history = await handle.fetch_history()
    assert removed == [
        TriagePod(f"ymir-triage-{handle.first_execution_run_id}", request.analysis.jira_issue, True)
    ]
    scheduled = (
        event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    )
    activity_queues = {event.activity_type.name: event.task_queue.name for event in scheduled}
    assert activity_queues["provision_triage_pod"] == activity_queues["delete_triage_pod"] == queue
    if failure == "lost":
        assert activity_queues["watch_triage_pod"] == queue
        assert activity_queues["read_publication_inputs"] == removed[0].task_queue
    else:
        assert set(activity_queues) == {"provision_triage_pod", "delete_triage_pod"}
