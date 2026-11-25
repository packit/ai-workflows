"""Temporal Activity names and controller registration for triage."""

from datetime import timedelta

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from ymir.agents.temporal_worker_pods import PodProvisioner, WorkerPod, WorkerPodActivities, WorkerPodConfig

TRIAGE_WORKER_POD = WorkerPodConfig(
    workflow="triage",
    module="ymir.agents.temporal_triage_worker",
    image="beeai-agent:c10s",
    jira_issue_env="YMIR_TRIAGE_ISSUE",
)


class TriagePodActivities(WorkerPodActivities):
    # Keep the names recorded in existing Temporal histories.
    @activity.defn(name="provision_triage_pod")
    async def provision_pod(self, pod: WorkerPod) -> None:
        await super().provision_pod(pod)

    @activity.defn(name="watch_triage_pod")
    async def watch_pod(self, pod: WorkerPod) -> None:
        await super().watch_pod(pod)

    @activity.defn(name="delete_triage_pod")
    async def delete_pod(self, pod: WorkerPod) -> None:
        await super().delete_pod(pod)


def create_triage_control_worker(client: Client, queue: str, provisioner: PodProvisioner) -> Worker:
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

    from ymir.agents.temporal_package_coordination import (
        PackageStreamActivities,
        PackageStreamWorkflow,
        plan_package_stream,
        release_coordinated_triage,
    )
    from ymir.agents.temporal_triage_ingress import INGRESS_ACTIVITIES, TriageIngressWorkflow
    from ymir.agents.temporal_triage_publication import TriageRequestReceiptWorkflow

    activities = TriagePodActivities(provisioner)
    coordination = PackageStreamActivities(client, queue)
    return Worker(
        client,
        task_queue=queue,
        workflows=[
            TriageRequestReceiptWorkflow,
            TriageIngressWorkflow,
            PackageStreamWorkflow,
        ],
        activities=[
            *INGRESS_ACTIVITIES,
            plan_package_stream,
            release_coordinated_triage,
            coordination.submit_package_coordination,
            coordination.submit_sibling_triage,
            activities.provision_pod,
            activities.watch_pod,
            activities.delete_pod,
        ],
        max_heartbeat_throttle_interval=timedelta(seconds=6),
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                TriageRequestReceiptWorkflow.__module__,
                TriageIngressWorkflow.__module__,
                PackageStreamWorkflow.__module__,
            )
        ),
    )
