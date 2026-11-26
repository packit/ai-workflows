"""Verify the control workflow owns startup failure and cancellation cleanup."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from flexmock import flexmock

from ymir.common.temporal_triage import TriageIntent, submit_triage_intent, triage_issue_workflow_id

pytest.importorskip("temporalio")

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError

from ymir.agents import temporal_triage_ingress as ingress
from ymir.agents.temporal_triage_provisioning import TRIAGE_WORKER_POD
from ymir.agents.temporal_triage_publication import PublicationInputs
from ymir.agents.temporal_triage_worker import create_triage_control_worker
from ymir.agents.temporal_worker_pods import (
    GATEWAY_URL,
    READY_FILE,
    OpenShiftPodProvisioner,
    PodmanPodProvisioner,
    WorkerPod,
    WorkerPodConfig,
)

ADDRESS = os.getenv("TEMPORAL_TEST_ADDRESS")
POD_TEMPLATE = Path("openshift/templates/temporal-worker-pod.yml")


@pytest.mark.asyncio
@pytest.mark.parametrize(("dry_run_only", "dry_run"), [(False, False), (False, True), (True, True)])
async def test_podman_pod_passes_request_mode_to_both_containers(dry_run_only, dry_run):
    provisioner = PodmanPodProvisioner(
        ("podman-remote",),
        "network",
        {},
        {},
        "temporal:7233",
        "testing",
        "controller",
        TRIAGE_WORKER_POD,
        dry_run_only=dry_run_only,
    )
    launched = {}

    async def launch(pod, slot, role, template, mounts, env, command):
        launched[role] = env

    async def reserve(pod):
        return "slot"

    async def create_volume(*args):
        assert args[:2] == ("volume", "create")

    flexmock(provisioner).should_receive("_slot").replace_with(reserve)
    flexmock(provisioner).should_receive("_run").replace_with(create_volume)
    flexmock(provisioner).should_receive("_container").replace_with(launch)
    pod = WorkerPod("ymir-triage-fixture", "RHEL-987654", dry_run)
    await provisioner.create(pod)
    assert launched["worker"]["TEMPORAL_TASK_QUEUE"] == pod.task_queue
    assert launched["worker"]["MCP_GATEWAY_URL"] == GATEWAY_URL
    assert launched["gateway"]["MCP_BIND_HOST"] == "127.0.0.1"
    for env in launched.values():
        assert (env.get("JIRA_DRY_RUN") == "true") == dry_run
        assert (env.get("MOCK_JIRA") == "true") == dry_run_only


def test_openshift_pod_shares_only_one_execution_workspace():
    backend = OpenShiftPodProvisioner(
        "testing", POD_TEMPLATE, "temporal.testing:7233", "testing", TRIAGE_WORKER_POD
    )
    pod = WorkerPod("ymir-triage-fixture", "RHEL-987654", True)
    manifest = backend.manifest(pod)
    assert manifest["metadata"]["labels"]["ymir-triage-execution"] == pod.name
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
    assert {"name": "YMIR_TRIAGE_ISSUE", "value": pod.jira_issue} in worker["env"]
    assert worker["image"] == "beeai-agent:c10s"
    assert worker["args"][-1] == "ymir.agents.temporal_triage_worker"


def test_shared_template_accepts_workflow_specific_settings():
    config = WorkerPodConfig(
        workflow="reproducer",
        module="fixture.worker",
        image="beeai-agent:c9s",
        deadline_seconds=5400,
        worker_env={"CONTAINER_VERSION": "c9s"},
        worker_resources={"requests": {"memory": "4Gi"}},
        gateway_env={"BUILD_BACKEND": "copr"},
        gateway_secrets=("testing-farm-env",),
    )
    backend = OpenShiftPodProvisioner("testing", POD_TEMPLATE, "temporal.testing:7233", "testing", config)
    pod = WorkerPod("ymir-reproducer-fixture", None, False)
    manifest = backend.manifest(pod)
    assert manifest["metadata"]["labels"] == {
        "app": "ymir-temporal-reproducer",
        "ymir-reproducer-execution": pod.name,
    }
    assert manifest["spec"]["activeDeadlineSeconds"] == 5400
    worker, gateway = manifest["spec"]["containers"]
    assert worker["image"] == "beeai-agent:c9s"
    assert worker["args"] == ["/usr/bin/python3", "-m", "fixture.worker"]
    assert worker["resources"] == config.worker_resources
    env = {entry["name"]: entry["value"] for entry in worker["env"]}
    assert env["CONTAINER_VERSION"] == "c9s"
    assert env["TEMPORAL_TASK_QUEUE"] == pod.task_queue and "YMIR_JIRA_ISSUE" not in env
    assert {"name": "BUILD_BACKEND", "value": "copr"} in gateway["env"]
    assert {"secretRef": {"name": "testing-farm-env"}} in gateway["envFrom"]
    assert {"secretRef": {"name": "testing-farm-env"}} not in worker["envFrom"]


@pytest.mark.asyncio
@pytest.mark.skipif(not ADDRESS, reason="Set TEMPORAL_TEST_ADDRESS to a local Temporal server")
@pytest.mark.parametrize("failure", ["startup", "cancel", "lost"])
async def test_provisioning_cleans_up_after_failure(failure, monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "test")
    monkeypatch.delenv("CHAT_MODEL_TRIAGE", raising=False)
    created = asyncio.Event()
    started = []
    removed = []
    ready = False

    async def read_publication_inputs(request):
        return PublicationInputs([], "New", request.dry_run)

    flexmock(ingress).should_receive("read_publication_inputs").replace_with(read_publication_inputs).once()

    class Backend:
        async def create(self, pod):
            started.append(pod)
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
    intent = TriageIntent(f"RHEL-{int(token, 16)}", token, "discovery", dry_run=True)
    async with create_triage_control_worker(client, queue, Backend()):
        assert await submit_triage_intent(client, intent, task_queue=queue) == "accepted"
        handle = client.get_workflow_handle(triage_issue_workflow_id(intent.issue))
        await asyncio.wait_for(created.wait(), timeout=15)
        if failure == "cancel":
            await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=20)
        history = await handle.fetch_history()
    [pod] = removed
    assert started == [pod] and pod.dry_run and pod.jira_issue == intent.issue
    scheduled = (
        event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    )
    activity_queues = {event.activity_type.name: event.task_queue.name for event in scheduled}
    assert activity_queues["provision_triage_pod"] == activity_queues["delete_triage_pod"] == queue
    if failure == "lost":
        assert activity_queues["watch_triage_pod"] == queue
        assert activity_queues["read_publication_inputs"] == pod.task_queue
    else:
        assert set(activity_queues) == {
            "prepare_triage_intent",
            "check_triage_intent",
            "provision_triage_pod",
            "delete_triage_pod",
        }


@pytest.mark.asyncio
async def test_in_cluster_provisioner_deletes_only_the_observed_pod_uid():
    calls = []
    pod = WorkerPod("ymir-triage-fixture", "RHEL-987654", True)

    class API:
        state = None

        async def request(self, method, name="", body=None):
            calls.append((method, name, body))
            if method == "POST":
                self.state = body
                self.state["metadata"]["uid"] = "fixture-uid"
                self.state["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
            elif method == "DELETE":
                self.state = None
            return self.state

    backend = OpenShiftPodProvisioner(
        "testing",
        POD_TEMPLATE,
        "temporal.testing:7233",
        "testing",
        TRIAGE_WORKER_POD,
        api=API(),
    )
    await backend.create(pod)
    assert await backend.ready(pod)
    await backend.delete(pod)
    assert [method for method, _, _ in calls] == ["GET", "POST", "GET", "GET", "DELETE", "GET"]
    assert calls[-2] == ("DELETE", pod.name, {"preconditions": {"uid": "fixture-uid"}})
