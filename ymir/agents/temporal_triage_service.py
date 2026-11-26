"""Run the shared triage worker in OpenShift or local Compose."""

import asyncio
import os
import signal
import socket
from pathlib import Path

from ymir.agents.temporal_triage_provisioning import TRIAGE_WORKER_POD
from ymir.agents.temporal_triage_worker import create_triage_control_worker
from ymir.agents.temporal_worker import connect_worker_client
from ymir.agents.temporal_worker_pods import InClusterPodAPI, OpenShiftPodProvisioner, PodmanPodProvisioner
from ymir.common.temporal_triage import CONTROL_QUEUE


async def main() -> None:
    ready = Path.home() / ".ymir-triage-controller-ready"
    ready.unlink(missing_ok=True)
    address = os.environ["TEMPORAL_ADDRESS"]
    queue = os.getenv("TEMPORAL_TRIAGE_CONTROL_QUEUE", CONTROL_QUEUE)
    client = await connect_worker_client(address, os.getenv("TEMPORAL_NAMESPACE", "default"))
    if os.getenv("YMIR_PODMAN_URL"):
        provisioner = await PodmanPodProvisioner.from_compose(
            socket.gethostname(), address, client.namespace, TRIAGE_WORKER_POD
        )
        for entry in provisioner.gateway["Config"]["Env"]:
            key, _, value = entry.partition("=")
            if key in {"JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN"} and value:
                os.environ.setdefault(key, value)
    else:
        namespace = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
        provisioner = OpenShiftPodProvisioner(
            namespace,
            Path(__file__).resolve().parents[2] / "temporal-worker-pod.yml",
            os.getenv("TEMPORAL_POD_ADDRESS") or address,
            client.namespace,
            TRIAGE_WORKER_POD,
            api=InClusterPodAPI(namespace),
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with create_triage_control_worker(client, queue, provisioner):
            ready.touch()
            await stop.wait()
    finally:
        ready.unlink(missing_ok=True)
        if isinstance(provisioner, PodmanPodProvisioner):
            await provisioner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
