"""Run the shared triage worker in OpenShift or local Compose."""

import asyncio
import os
from pathlib import Path

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor

from ymir.agents.observability import setup_observability
from ymir.agents.pydantic_agent import configured_model
from ymir.agents.temporal_triage_analysis import create_analysis_worker
from ymir.agents.temporal_triage_provisioning import (
    InClusterPodAPI,
    OpenShiftTriageProvisioner,
    create_triage_control_worker,
)
from ymir.agents.utils import resolve_chat_model_override
from ymir.common.temporal_triage import CONTROL_QUEUE


async def main() -> None:
    ready = Path.home() / ".ymir-triage-controller-ready"
    ready.unlink(missing_ok=True)
    address = os.environ["TEMPORAL_ADDRESS"]
    queue = os.getenv("TEMPORAL_TRIAGE_CONTROL_QUEUE", CONTROL_QUEUE)
    if endpoint := os.getenv("COLLECTOR_ENDPOINT"):
        setup_observability(endpoint)
    client = await Client.connect(
        address,
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        plugins=[PydanticAIPlugin()],
        interceptors=[TracingInterceptor()],
    )
    if os.getenv("TEMPORAL_TRIAGE_INLINE", "").lower() == "true":
        resolve_chat_model_override("triage")
        async with configured_model() as model:
            worker = await create_analysis_worker(
                client, queue, model, publication=True, ingress=True, gateway_connection_attempts=30
            )
            await worker.run()
        return

    namespace = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
    provisioner = OpenShiftTriageProvisioner(
        namespace,
        Path(__file__).resolve().parents[2] / "temporal-triage-pod.yml",
        os.getenv("TEMPORAL_POD_ADDRESS") or address,
        client.namespace,
        api=InClusterPodAPI(namespace),
    )
    async with create_triage_control_worker(client, queue, provisioner):
        ready.touch()
        try:
            await asyncio.Future()
        finally:
            ready.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
