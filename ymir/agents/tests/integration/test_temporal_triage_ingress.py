"""Test concurrent triage submissions against Temporal."""

import asyncio
import os
from uuid import uuid4

import pytest
from flexmock import flexmock
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from ymir.agents import temporal_triage_ingress as ingress
from ymir.agents.temporal_triage_analysis import TriageAnalysisRequest
from ymir.agents.temporal_triage_ingress import TriageIngressWorkflow
from ymir.agents.temporal_triage_publication import TriagePublicationRequest, TriageRequestReceiptWorkflow
from ymir.agents.temporal_worker_pods import WorkerPod
from ymir.common.constants import JiraLabels
from ymir.common.temporal_triage import TriageIntent, submit_triage_intent, triage_issue_workflow_id

ADDRESS = os.getenv("TEMPORAL_TEST_ADDRESS")
pytestmark = pytest.mark.skipif(not ADDRESS, reason="Set TEMPORAL_TEST_ADDRESS to a local Temporal server")


@pytest.mark.asyncio
async def test_ingress_serializes_requests_and_reports_failure(monkeypatch):
    client = await Client.connect(ADDRESS, plugins=[PydanticAIPlugin()])
    publisher = await Client.connect(ADDRESS)
    queue = f"triage-ingress-test-{uuid4().hex}"
    issue = f"RHEL-{uuid4().int}"
    monkeypatch.setenv("TEMPORAL_TRIAGE_CONTROL_QUEUE", queue)
    first_provisioned = asyncio.Event()
    release_first = asyncio.Event()
    steps = []
    pods = set()
    provisioning_attempts = []

    @activity.defn(name="prepare_triage_intent")
    async def prepare(intent: TriageIntent) -> TriagePublicationRequest:
        steps.append(("prepare", intent.request_id))
        return TriagePublicationRequest(
            TriageAnalysisRequest(issue, {}, 1, user_triggered=intent.triggered_by == "user"),
            intent.request_id,
            dry_run=intent.dry_run,
        )

    @activity.defn(name="check_triage_intent")
    async def check(request: TriagePublicationRequest, intent: TriageIntent) -> str | None:
        if intent.triggers:
            return "Fixture manual triage needs no analysis."
        return None

    @activity.defn(name="acknowledge_triage_request")
    async def acknowledge(request: TriagePublicationRequest, skipped: str | None) -> None:
        steps.append(("ack", request.request_id, skipped))

    @activity.defn(name="begin_triage_intent")
    async def begin(request: TriagePublicationRequest, intent: TriageIntent, skipped: bool) -> None:
        steps.append(("begin", intent.request_id, skipped))

    @activity.defn(name="provision_triage_pod")
    async def provision(pod: WorkerPod) -> None:
        attempt = activity.info().attempt
        provisioning_attempts.append(attempt)
        if attempt == 1:
            assert not pods
            pods.add(pod.name)
            steps.append(("provision", pod.name))
            first_provisioned.set()
            await release_first.wait()
        else:
            assert pods == {pod.name}
        raise ApplicationError("Fixture pod startup failed")

    @activity.defn(name="delete_triage_pod")
    async def delete(pod: WorkerPod) -> None:
        pods.remove(pod.name)
        steps.append(("delete", pod.name))

    async def post_comment(request: TriagePublicationRequest, step: str, body: str) -> None:
        assert step == "failed"
        steps.append(("failure", request.request_id, body))

    async def apply_labels(request: TriagePublicationRequest, add: list[str], remove: list[str]) -> None:
        assert add == [JiraLabels.TRIAGE_ERRORED.value]
        assert remove == [JiraLabels.TRIAGE_IN_PROGRESS.value]

    flexmock(ingress).should_receive("_post_comment").replace_with(post_comment).once()
    flexmock(ingress).should_receive("apply_triage_labels").replace_with(apply_labels).once()

    first = TriageIntent(issue, "cli:request", "user", dry_run=False)
    automatic = TriageIntent(issue.lower(), "discovery:fixture", "discovery", dry_run=False)
    manual = TriageIntent(
        issue,
        "jira:event-1",
        "user",
        dry_run=False,
        triggers={JiraLabels.TODO.value: "1"},
    )
    handle = publisher.get_workflow_handle(triage_issue_workflow_id(issue))
    async with Worker(
        client,
        task_queue=queue,
        workflows=[TriageIngressWorkflow, TriageRequestReceiptWorkflow],
        activities=[prepare, check, acknowledge, begin, provision, delete, ingress.record_triage_failure],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                TriageIngressWorkflow.__module__, TriageRequestReceiptWorkflow.__module__
            )
        ),
    ):
        assert await submit_triage_intent(publisher, first) == "accepted"
        await asyncio.wait_for(first_provisioned.wait(), timeout=15)
        active = await handle.describe()
        assert active.status == WorkflowExecutionStatus.RUNNING
        history = await handle.fetch_history()
        first_update = next(
            event.workflow_execution_update_accepted_event_attributes
            for event in history.events
            if event.HasField("workflow_execution_update_accepted_event_attributes")
            and event.workflow_execution_update_accepted_event_attributes.protocol_instance_id
            == first.update_id
        )
        assert first_update.accepted_request.input.name == "enqueue"
        assert await submit_triage_intent(publisher, automatic) == "coalesced"
        manual_result = asyncio.create_task(submit_triage_intent(publisher, manual, wait=True))
        async with asyncio.timeout(10):
            while True:
                history = await handle.fetch_history()
                if any(
                    event.HasField("workflow_execution_update_accepted_event_attributes")
                    and event.workflow_execution_update_accepted_event_attributes.protocol_instance_id
                    == manual.update_id
                    for event in history.events
                ):
                    break
                await asyncio.sleep(0.05)
        assert not manual_result.done()
        assert (await handle.describe()).run_id == active.run_id
        assert ("prepare", manual.request_id) not in steps
        release_first.set()
        with pytest.raises(WorkflowFailureError, match="Workflow execution failed"):
            await asyncio.wait_for(handle.result(), timeout=20)
        assert await manual_result == {
            "status": "skipped",
            "detail": "Fixture manual triage needs no analysis.",
            "output": None,
        }
        assert not pods
        assert provisioning_attempts == [1, 2]
        assert [(step[0], step[1]) for step in steps if step[0] not in {"provision", "delete"}] == [
            ("prepare", first.request_id),
            ("ack", first.request_id),
            ("begin", first.request_id),
            ("failure", first.request_id),
            ("prepare", manual.request_id),
            ("ack", manual.request_id),
            ("begin", manual.request_id),
        ]
        failure = next(step for step in steps if step[0] == "failure")
        assert active.run_id in failure[2]
        assert '"kind": "triage-failure"' in failure[2]
        repeated = await submit_triage_intent(publisher, manual, wait=True)
        assert repeated["status"] in {"skipped", "duplicate"}
        # A repeated Update can return its earlier response, even after the
        # workflow closed. The receipt also guards against a new Workflow Run.
        latest = await handle.describe()
        if latest.run_id != active.run_id:
            retry_handle = publisher.get_workflow_handle(handle.id, run_id=latest.run_id)
            assert await retry_handle.result() == [
                {"request_id": manual.request_id, "status": "duplicate", "detail": None, "preview": None}
            ]
        follow_up = TriageIntent(
            issue,
            "jira:event-2",
            "user",
            dry_run=False,
            triggers={JiraLabels.TODO.value: "2"},
        )
        assert await submit_triage_intent(publisher, follow_up) == "accepted"
        latest = await handle.describe()
        assert latest.run_id != active.run_id
        follow_up_handle = publisher.get_workflow_handle(handle.id, run_id=latest.run_id)
        result = await asyncio.wait_for(follow_up_handle.result(), timeout=20)
        assert [(item["request_id"], item["status"]) for item in result] == [
            (follow_up.request_id, "skipped")
        ]
        assert len([step for step in steps if step[:2] == ("ack", manual.request_id)]) == 1
