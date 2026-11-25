"""Control-worker Activities and per-run pod lifecycle for Temporal triage."""

import asyncio
import json
import os
import ssl
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

import aiohttp
import yaml
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

PODMAN_LIMIT = 3
EXECUTION_LABEL = "ymir-triage-execution"
SUITE_LABEL = "ymir-triage-suite"
WORKSPACE = "/git-repos"
READY_FILE = f"{WORKSPACE}/.triage-worker-ready"
GATEWAY_URL = "http://127.0.0.1:8000/sse"
POLL_SECONDS = 3

WORKER_MOUNTS = {
    "/home/beeai/ymir",
    "/home/beeai/rhel-config.json",
    "/home/beeai/jotnar-vertex-dev.json",
    "/home/beeai/mock_repos",
}
GATEWAY_MOUNTS = {
    "/home/mcp/ymir/tools",
    "/home/mcp/ymir/common",
    "/home/mcp/ymir/tools/privileged/tests/data",
    "/home/mcp/keytab",
    "/home/mcp/rhel-config.json",
    "/home/mcp/konflux-token",
}


@dataclass(frozen=True)
class TriagePod:
    name: str
    jira_issue: str
    dry_run: bool

    @property
    def task_queue(self) -> str:
        return self.name


class PodProvisioner(Protocol):
    async def create(self, pod: TriagePod) -> None: ...
    async def ready(self, pod: TriagePod) -> bool: ...
    async def delete(self, pod: TriagePod) -> None: ...


class ContainerCommandError(RuntimeError):
    pass


async def _command(*command: str, data: str | None = None, check: bool = True) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(45):
            stdout, stderr = await process.communicate(data.encode() if data is not None else None)
    except (asyncio.CancelledError, TimeoutError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if check and process.returncode:
        raise ContainerCommandError(f"{command[0]} {command[1]} failed: {stderr.decode().strip()}")
    return process.returncode, stdout.decode()


def _worker_env(pod: TriagePod, address: str, namespace: str, dry_run: bool) -> dict[str, str]:
    return {
        "TEMPORAL_ADDRESS": address,
        "TEMPORAL_NAMESPACE": namespace,
        "TEMPORAL_TASK_QUEUE": pod.task_queue,
        "YMIR_TRIAGE_ISSUE": pod.jira_issue,
        "MCP_GATEWAY_URL": GATEWAY_URL,
        "GIT_REPO_BASEPATH": WORKSPACE,
        "DRY_RUN": str(dry_run).lower(),
    }


class PodmanTriageProvisioner:
    """Use three atomic Podman names to cap local pods across test controllers."""

    def __init__(
        self,
        command: tuple[str, ...],
        network: str,
        worker: dict,
        gateway: dict,
        address: str,
        namespace: str,
        suite: str,
    ):
        self.command = command
        self.network = network
        self.worker = worker
        self.gateway = gateway
        self.address = address
        self.namespace = namespace
        self.suite = suite
        self.lock = asyncio.Lock()

    @classmethod
    async def for_e2e(cls, controller: str, endpoint: str, namespace: str, *, local: bool):
        command = ("podman-remote", "--url", os.environ["YMIR_PODMAN_URL"])
        _, raw = await _command(*command, "container", "inspect", controller)
        worker = json.loads(raw)[0]
        network, settings = next(iter(worker["NetworkSettings"]["Networks"].items()))
        if local:
            endpoint = f"{settings['IPAddress']}:{endpoint.rsplit(':', 1)[1]}"
        project = worker["Config"]["Labels"]["com.docker.compose.project"]
        _, raw = await _command(
            *command,
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=mcp-gateway",
            "--format",
            "{{.ID}}",
        )
        (gateway_id,) = raw.split()
        _, raw = await _command(*command, "container", "inspect", gateway_id)
        return cls(command, network, worker, json.loads(raw)[0], endpoint, namespace, controller)

    async def _run(self, *args: str, check: bool = True):
        return await _command(*self.command, *args, check=check)

    async def _inspect(self, resource: str, name: str) -> dict | None:
        code, raw = await self._run(resource, "inspect", name, check=False)
        if not code:
            value = json.loads(raw)
            return value[0] if isinstance(value, list) else value
        exists, _ = await self._run(resource, "exists", name, check=False)
        if exists == 1:
            return None
        raise ContainerCommandError(f"Could not inspect Podman {resource} {name}")

    async def _slot(self, pod: TriagePod) -> str | None:
        free = []
        for i in range(PODMAN_LIMIT):
            slot = f"ymir-triage-slot-{i + 1}"
            state = await self._inspect("pod", slot)
            if state is None:
                free.append(slot)
                continue
            labels = state.get("Labels") or {}
            if labels.get(EXECUTION_LABEL) == pod.name:
                return slot
            suite, execution = labels.get(SUITE_LABEL), labels.get(EXECUTION_LABEL)
            if suite and suite.startswith("ymir-triage-e2e-") and execution:
                controller = await self._inspect("container", suite)
                if controller is None or not controller["State"]["Running"]:
                    await self._run("pod", "rm", "--force", "--ignore", state["Id"])
                    await self._delete_workspace(execution, suite)
                    free.append(slot)
        for slot in free:
            status, _ = await self._run(
                "pod",
                "create",
                "--name",
                slot,
                "--network",
                self.network,
                "--label",
                f"{EXECUTION_LABEL}={pod.name}",
                "--label",
                f"{SUITE_LABEL}={self.suite}",
                check=False,
            )
            state = await self._inspect("pod", slot)
            if state and (state.get("Labels") or {}).get(EXECUTION_LABEL) == pod.name:
                return slot
            if status and state is None:
                raise ContainerCommandError(f"Could not reserve Podman slot {slot}")
        return None

    async def _container(
        self,
        pod: TriagePod,
        slot: str,
        role: str,
        template: dict,
        mounts: set[str],
        env: dict[str, str],
        command: tuple[str, ...],
    ) -> None:
        name = f"{pod.name}-{role}"
        if await self._inspect("container", name):
            return
        config = dict(item.split("=", 1) for item in template["Config"]["Env"])
        for key in ("HOSTNAME", "YMIR_PODMAN_URL", "YMIR_TRIAGE_CONTROLLER", "TEMPORAL_TRIAGE_ADDRESS"):
            config.pop(key, None)
        config.update(env)
        args = []
        for entry in template["Mounts"]:
            if entry["Destination"] in mounts:
                args.extend(("-v", f"{entry['Source']}:{entry['Destination']}:ro,z"))
        # The remote CLI reads the env file on the controller. Keep credentials
        # out of OS command arguments and never mount the controller socket.
        with tempfile.NamedTemporaryFile(mode="w") as env_file:
            for key, value in config.items():
                if "\n" in value or "\r" in value:
                    raise ValueError(f"Cannot pass multiline environment setting {key} to Podman")
                print(f"{key}={value}", file=env_file)
            env_file.flush()
            await self._run(
                "run",
                "--detach",
                "--name",
                name,
                "--pod",
                slot,
                "--restart",
                "on-failure:2",
                "--env-file",
                env_file.name,
                "-v",
                f"{pod.name}:{WORKSPACE}:U,z",
                *args,
                template["Image"],
                *command,
            )

    async def create(self, pod: TriagePod) -> None:
        while True:
            async with self.lock:
                if slot := await self._slot(pod):
                    await self._run(
                        "volume", "create", "--ignore", "--label", f"{SUITE_LABEL}={self.suite}", pod.name
                    )
                    break
            activity.heartbeat()
            await asyncio.sleep(POLL_SECONDS)
        # Podman provisioning exists only for dry-run E2E, using mock Jira.
        if not pod.dry_run:
            raise ApplicationError("Local triage pods require dry-run requests", non_retryable=True)
        common = {
            "DRY_RUN": "true",
            "JIRA_DRY_RUN": "true",
            "MOCK_JIRA": "true",
            "GIT_REPO_BASEPATH": WORKSPACE,
        }
        await self._container(
            pod,
            slot,
            "gateway",
            self.gateway,
            GATEWAY_MOUNTS,
            common
            | {
                "MCP_BIND_HOST": "127.0.0.1",
                "SSE_PORT": "8000",
                "GIT_CONFIG_GLOBAL": f"{WORKSPACE}/.mock_gitconfig",
                "ALLOW_RHEL_CONFIG_OVERRIDE": "true",
            },
            ("python", "-m", "ymir.tools.privileged.gateway"),
        )
        await self._container(
            pod,
            slot,
            "worker",
            self.worker,
            WORKER_MOUNTS,
            common | _worker_env(pod, self.address, self.namespace, True),
            ("/usr/bin/python3", "-m", "ymir.agents.temporal_triage_worker"),
        )

    async def ready(self, pod: TriagePod) -> bool:
        for role in ("worker", "gateway"):
            container = await self._inspect("container", f"{pod.name}-{role}")
            if container is None:
                return False
            state = container["State"]
            if state["Status"] == "exited" and (container["RestartCount"] >= 2 or state["ExitCode"] == 0):
                raise ApplicationError(
                    f"Podman {role} for {pod.name} exited with code {state['ExitCode']}", non_retryable=True
                )
            if not state["Running"]:
                return False
        code, _ = await self._run("exec", f"{pod.name}-worker", "test", "-f", READY_FILE, check=False)
        return code == 0

    async def delete(self, pod: TriagePod) -> None:
        async with self.lock:
            for i in range(PODMAN_LIMIT):
                slot = f"ymir-triage-slot-{i + 1}"
                state = await self._inspect("pod", slot)
                if state and (state.get("Labels") or {}).get(EXECUTION_LABEL) == pod.name:
                    await self._run("pod", "rm", "--force", "--ignore", state["Id"])
            await self._delete_workspace(pod.name, self.suite)

    async def _delete_workspace(self, name: str, suite: str) -> None:
        volume = await self._inspect("volume", name)
        if volume and (volume.get("Labels") or {}).get(SUITE_LABEL) == suite:
            code, _ = await self._run("volume", "rm", name, check=False)
            if code and await self._inspect("volume", name):
                raise ContainerCommandError(f"Could not remove triage workspace {name}")


class InClusterPodAPI:
    """Use the controller's projected credentials for Kubernetes Pod operations."""

    def __init__(self, namespace: str):
        directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.token = directory / "token"
        self.ssl_context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT_HTTPS"]
        if ":" in host:
            host = f"[{host}]"
        self.url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods"

    async def request(self, method: str, name: str = "", body: dict | None = None) -> dict | None:
        url = f"{self.url}/{name}" if name else self.url
        headers = {"Authorization": f"Bearer {self.token.read_text().strip()}"}
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session,
            session.request(
                method, url, json=body, headers=headers, ssl=self.ssl_context, allow_redirects=False
            ) as response,
        ):
            if response.status == 404 and name:
                return None
            if not 200 <= response.status < 300:
                raise ContainerCommandError(f"Kubernetes Pod {method} returned {response.status}")
            return await response.json()


class OpenShiftTriageProvisioner:
    """Run the same worker and gateway in a namespaced OpenShift pod."""

    def __init__(
        self,
        namespace: str,
        template: Path,
        address: str,
        temporal_namespace: str,
        *,
        api: InClusterPodAPI | None = None,
    ):
        self.api = api
        self.namespace = namespace
        self.template = template
        self.address = address
        self.temporal_namespace = temporal_namespace

    async def _run(self, *args: str, data: str | None = None, check: bool = True):
        return await _command("oc", "--namespace", self.namespace, *args, data=data, check=check)

    def manifest(self, pod: TriagePod) -> dict:
        manifest = yaml.safe_load(self.template.read_text())
        if (manifest.get("apiVersion"), manifest.get("kind")) != ("v1", "Pod"):
            raise ValueError("Triage execution template must describe a v1 Pod")
        metadata = manifest.setdefault("metadata", {})
        metadata["name"] = pod.name
        metadata["namespace"] = self.namespace
        metadata.setdefault("labels", {})[EXECUTION_LABEL] = pod.name
        for container in manifest["spec"]["containers"]:
            env = {"GIT_REPO_BASEPATH": WORKSPACE}
            if pod.dry_run:
                env |= {"DRY_RUN": "true", "JIRA_DRY_RUN": "true"}
            if container["name"] == "worker":
                env |= _worker_env(pod, self.address, self.temporal_namespace, pod.dry_run)
            elif container["name"] == "gateway":
                env |= {"MCP_BIND_HOST": "127.0.0.1"}
            container["env"] = [
                *(item for item in container.get("env", []) if item["name"] not in env),
                *({"name": key, "value": value} for key, value in env.items()),
            ]
        return manifest

    async def _inspect(self, pod: TriagePod) -> dict | None:
        if self.api:
            state = await self.api.request("GET", pod.name)
            if state is None:
                return None
        else:
            _, raw = await self._run("get", "pod", pod.name, "-o", "json", "--ignore-not-found")
            if not raw.strip():
                return None
            state = json.loads(raw)
        if state["metadata"].get("labels", {}).get(EXECUTION_LABEL) != pod.name:
            raise ApplicationError(f"Pod name {pod.name} belongs to another execution", non_retryable=True)
        return state

    async def create(self, pod: TriagePod) -> None:
        if not await self._inspect(pod):
            try:
                manifest = self.manifest(pod)
                if self.api:
                    await self.api.request("POST", body=manifest)
                else:
                    await self._run("create", "-f", "-", data=json.dumps(manifest))
            except ContainerCommandError:
                if not await self._inspect(pod):
                    raise

    async def ready(self, pod: TriagePod) -> bool:
        state = await self._inspect(pod)
        if state is None or state["metadata"].get("deletionTimestamp"):
            return False
        status = state.get("status", {})
        if status.get("phase") in {"Failed", "Succeeded"}:
            raise ApplicationError(f"Execution pod {pod.name} exited", non_retryable=True)
        return any(c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", []))

    async def delete(self, pod: TriagePod) -> None:
        if not (state := await self._inspect(pod)):
            return
        if not self.api:
            await self._run("delete", "pod", pod.name, "--ignore-not-found", "--timeout=35s")
            return
        await self.api.request("DELETE", pod.name, {"preconditions": {"uid": state["metadata"]["uid"]}})
        async with asyncio.timeout(40):
            while await self._inspect(pod):
                await asyncio.sleep(POLL_SECONDS)


class TriagePodActivities:
    def __init__(self, provisioner: PodProvisioner):
        self.provisioner = provisioner

    @activity.defn
    async def provision_triage_pod(self, pod: TriagePod) -> None:
        activity.heartbeat()
        await self.provisioner.create(pod)
        while not await self.provisioner.ready(pod):
            activity.heartbeat()
            await asyncio.sleep(POLL_SECONDS)

    @activity.defn
    async def watch_triage_pod(self, pod: TriagePod) -> None:
        unavailable = 0
        while True:
            activity.heartbeat()
            if await self.provisioner.ready(pod):
                unavailable = 0
            else:
                unavailable += 1
                if unavailable * POLL_SECONDS >= 30:
                    raise ApplicationError(
                        f"Execution pod {pod.name} lost its worker or gateway", non_retryable=True
                    )
            await asyncio.sleep(POLL_SECONDS)

    @activity.defn
    async def delete_triage_pod(self, pod: TriagePod) -> None:
        await self.provisioner.delete(pod)


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
            activities.provision_triage_pod,
            activities.watch_triage_pod,
            activities.delete_triage_pod,
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
