"""Configure the control and analysis workers for Temporal triage."""

import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from pydantic_ai.models import Model
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from ymir.agents import temporal_triage_analysis as analysis
from ymir.agents.pydantic_agent import configured_model
from ymir.agents.temporal_package_coordination import (
    PackageStreamActivities,
    PackageStreamWorkflow,
    plan_package_stream,
    release_coordinated_triage,
)
from ymir.agents.temporal_triage_ingress import INGRESS_ACTIVITIES, TriageIngressWorkflow
from ymir.agents.temporal_triage_provisioning import TriagePodActivities
from ymir.agents.temporal_triage_publication import PUBLICATION_ACTIVITIES, TriageRequestReceiptWorkflow
from ymir.agents.temporal_worker import serve_worker_pod
from ymir.agents.temporal_worker_pods import PodProvisioner
from ymir.agents.utils import resolve_chat_model_override
from ymir.common.mock_repos import cleanup_mock_gitconfig, setup_mock_repos_from_env

CONTROL_WORKFLOWS = [TriageRequestReceiptWorkflow, TriageIngressWorkflow, PackageStreamWorkflow]


def _control_activities(client: Client, queue: str):
    coordination = PackageStreamActivities(client, queue)
    return [
        *INGRESS_ACTIVITIES,
        plan_package_stream,
        release_coordinated_triage,
        coordination.submit_package_coordination,
        coordination.submit_sibling_triage,
    ]


def create_triage_control_worker(client: Client, queue: str, provisioner: PodProvisioner) -> Worker:
    pods = TriagePodActivities(provisioner)
    return Worker(
        client,
        task_queue=queue,
        workflows=CONTROL_WORKFLOWS,
        activities=[*_control_activities(client, queue), pods.provision_pod, pods.watch_pod, pods.delete_pod],
        max_heartbeat_throttle_interval=timedelta(seconds=6),
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                *(definition.__module__ for definition in CONTROL_WORKFLOWS)
            )
        ),
    )


async def create_analysis_worker(
    client: Client,
    task_queue: str,
    model: Model,
    *,
    gateway_connection_attempts: int = 1,
    ingress: bool = False,
) -> Worker:
    plugins = await analysis.configure_triage_agents(model, gateway_connection_attempts)
    workflows = [analysis.TriageAnalysisWorkflow]
    activities = [
        analysis.read_eligibility,
        analysis.prepare_publication_analysis,
        analysis.normalize_version,
        analysis.read_rebase_author,
        analysis.resolve_target,
        analysis.prepare_applicability,
        analysis.cleanup_applicability,
        analysis.read_buildroot,
        *PUBLICATION_ACTIVITIES,
    ]
    if ingress:
        workflows.extend(CONTROL_WORKFLOWS)
        activities.extend(_control_activities(client, task_queue))
    return Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=list(dict.fromkeys(activities)),
        plugins=plugins,
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                *(definition.__module__ for definition in workflows)
            )
        ),
    )


@asynccontextmanager
async def start_triage_worker(workspace: Path, client: Client, task_queue: str):
    issue = os.environ["YMIR_TRIAGE_ISSUE"]
    if os.environ.get("MOCK_REPOS_DIR") and (
        os.getenv("MOCK_JIRA", "").lower() == "true" and os.getenv("DRY_RUN", "").lower() == "true"
    ):
        clones = workspace / "mock-repos"
        prepared = clones / ".prepared"
        if prepared.exists():
            os.environ["MOCK_BLOCKED_URLS"] = prepared.read_text()
        else:
            shutil.rmtree(clones, ignore_errors=True)
            clones.mkdir()
            cleanup_mock_gitconfig()
            await asyncio.to_thread(setup_mock_repos_from_env, issue, clones)
            prepared.write_text(os.getenv("MOCK_BLOCKED_URLS", ""))
    resolve_chat_model_override("triage")
    async with (
        configured_model() as model,
        await create_analysis_worker(client, task_queue, model, gateway_connection_attempts=30),
    ):
        yield


if __name__ == "__main__":
    asyncio.run(serve_worker_pod(start_triage_worker))
