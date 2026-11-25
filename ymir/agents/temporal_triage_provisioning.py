"""Pod configuration and Temporal activity names for triage."""

from temporalio import activity

from ymir.agents.temporal_worker_pods import WorkerPod, WorkerPodActivities, WorkerPodConfig

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
