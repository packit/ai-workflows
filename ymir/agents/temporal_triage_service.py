"""Run the shared triage worker in OpenShift or local Compose."""

import asyncio
import os
from pathlib import Path

from ymir.agents.pydantic_agent import configured_model
from ymir.agents.temporal_triage_analysis import create_analysis_worker
from ymir.agents.temporal_triage_provisioning import TRIAGE_WORKER_POD, create_triage_control_worker
from ymir.agents.temporal_worker import connect_worker_client
from ymir.agents.temporal_worker_pods import InClusterPodAPI, OpenShiftPodProvisioner
from ymir.agents.utils import resolve_chat_model_override
from ymir.common.temporal_triage import CONTROL_QUEUE


async def main() -> None:
    ready = Path.home() / ".ymir-triage-controller-ready"
    ready.unlink(missing_ok=True)
    address = os.environ["TEMPORAL_ADDRESS"]
    queue = os.getenv("TEMPORAL_TRIAGE_CONTROL_QUEUE", CONTROL_QUEUE)
    client = await connect_worker_client(address, os.getenv("TEMPORAL_NAMESPACE", "default"))
    if os.getenv("TEMPORAL_TRIAGE_INLINE", "").lower() == "true":
        resolve_chat_model_override("triage")
        async with configured_model() as model:
            worker = await create_analysis_worker(
                client, queue, model, ingress=True, gateway_connection_attempts=30
            )
            await worker.run()
        return

    namespace = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
    provisioner = OpenShiftPodProvisioner(
        namespace,
        Path(__file__).resolve().parents[2] / "temporal-worker-pod.yml",
        os.getenv("TEMPORAL_POD_ADDRESS") or address,
        client.namespace,
        TRIAGE_WORKER_POD,
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
