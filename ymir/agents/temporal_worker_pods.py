"""Per-run Temporal worker pods on OpenShift and local Podman."""

import asyncio
import json
import os
import ssl
import tempfile
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import aiohttp
import yaml
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, FailureError

PODMAN_LIMIT = 3
WORKSPACE = "/git-repos"
READY_FILE = f"{WORKSPACE}/.temporal-worker-ready"
GATEWAY_URL = "http://127.0.0.1:8000/sse"
POLL_SECONDS = 3
Result = TypeVar("Result")

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
CHOWN_MOUNTS = {
    "/home/beeai/rhel-config.json",
    "/home/beeai/jotnar-vertex-dev.json",
    "/home/mcp/keytab",
    "/home/mcp/rhel-config.json",
    "/home/mcp/konflux-token",
}


@dataclass(frozen=True)
class WorkerPod:
    name: str
    jira_issue: str | None
    dry_run: bool

    @property
    def task_queue(self) -> str:
        return self.name


@dataclass(frozen=True)
class WorkerPodConfig:
    """Deployment settings supplied by the workflow's controller."""

    workflow: str
    module: str
    image: str
    jira_issue_env: str = "YMIR_JIRA_ISSUE"
    deadline_seconds: int = 3000
    worker_env: dict[str, str] = field(default_factory=dict)
    worker_resources: dict[str, dict[str, str]] | None = None
    gateway_env: dict[str, str] = field(default_factory=dict)
    gateway_secrets: tuple[str, ...] = ()

    @property
    def command(self) -> tuple[str, ...]:
        return "/usr/bin/python3", "-m", self.module

    @property
    def execution_label(self) -> str:
        return f"ymir-{self.workflow}-execution"


class PodProvisioner(Protocol):
    async def create(self, pod: WorkerPod) -> None: ...
    async def ready(self, pod: WorkerPod) -> bool: ...
    async def delete(self, pod: WorkerPod) -> None: ...


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


def _worker_env(pod: WorkerPod, config: WorkerPodConfig, address: str, namespace: str) -> dict[str, str]:
    return {
        **config.worker_env,
        "TEMPORAL_ADDRESS": address,
        "TEMPORAL_NAMESPACE": namespace,
        "TEMPORAL_TASK_QUEUE": pod.task_queue,
        **({config.jira_issue_env: pod.jira_issue} if pod.jira_issue is not None else {}),
        "MCP_GATEWAY_URL": GATEWAY_URL,
        "GIT_REPO_BASEPATH": WORKSPACE,
        "DRY_RUN": str(pod.dry_run).lower(),
    }


class PodmanPodProvisioner:
    """Reserve three Podman slots across local controllers for each workflow."""

    def __init__(
        self,
        command: tuple[str, ...],
        network: str,
        worker: dict,
        gateway: dict,
        address: str,
        namespace: str,
        suite: str,
        config: WorkerPodConfig,
        *,
        dry_run_only: bool = False,
    ):
        self.command = command
        self.network = network
        self.worker = worker
        self.gateway = gateway
        self.address = address
        self.namespace = namespace
        self.suite = suite
        self.config = config
        self.dry_run_only = dry_run_only
        self.suite_label = f"ymir-{config.workflow}-suite"
        self.slots = [f"ymir-{config.workflow}-slot-{i + 1}" for i in range(PODMAN_LIMIT)]
        self.lock = asyncio.Lock()

    @classmethod
    async def from_compose(
        cls,
        controller: str,
        endpoint: str,
        namespace: str,
        config: WorkerPodConfig,
        *,
        temporal_in_controller: bool = False,
        dry_run_only: bool = False,
    ):
        command = ("podman-remote", "--url", os.environ["YMIR_PODMAN_URL"])
        _, raw = await _command(*command, "container", "inspect", controller)
        worker = json.loads(raw)[0]
        network, settings = next(iter(worker["NetworkSettings"]["Networks"].items()))
        if temporal_in_controller:
            endpoint = f"{settings['IPAddress']}:{endpoint.rsplit(':', 1)[1]}"
        project = worker["Config"]["Labels"]["com.docker.compose.project"]
        env = dict(entry.split("=", 1) for entry in worker["Config"]["Env"])
        gateway_service = urlsplit(env["MCP_GATEWAY_URL"]).hostname
        _, raw = await _command(
            *command,
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={gateway_service}",
            "--format",
            "{{.ID}}",
        )
        gateway_ids = raw.split()
        if len(gateway_ids) != 1:
            raise ContainerCommandError(f"Expected one running {gateway_service} service in {project}")
        (gateway_id,) = gateway_ids
        _, raw = await _command(*command, "container", "inspect", gateway_id)
        return cls(
            command,
            network,
            worker,
            json.loads(raw)[0],
            endpoint,
            namespace,
            worker["Name"].lstrip("/"),
            config,
            dry_run_only=dry_run_only,
        )

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

    async def _slot(self, pod: WorkerPod) -> str | None:
        free = []
        for slot in self.slots:
            state = await self._inspect("pod", slot)
            if state is None:
                free.append(slot)
                continue
            labels = state.get("Labels") or {}
            if labels.get(self.config.execution_label) == pod.name:
                return slot
            suite, execution = labels.get(self.suite_label), labels.get(self.config.execution_label)
            if suite and execution:
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
                f"{self.config.execution_label}={pod.name}",
                "--label",
                f"{self.suite_label}={self.suite}",
                check=False,
            )
            state = await self._inspect("pod", slot)
            if state and (state.get("Labels") or {}).get(self.config.execution_label) == pod.name:
                return slot
            if status and state is None:
                raise ContainerCommandError(f"Could not reserve Podman slot {slot}")
        return None

    async def _container(
        self,
        pod: WorkerPod,
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
        for key in (
            "HOSTNAME",
            "YMIR_PODMAN_URL",
            f"TEMPORAL_{self.config.workflow.upper().replace('-', '_')}_ADDRESS",
        ):
            config.pop(key, None)
        config.update(env)
        args = []
        for entry in template["Mounts"]:
            target = entry["Destination"]
            if target in mounts:
                mode = "rw" if not self.dry_run_only and entry.get("RW") else "ro"
                options = f"{mode},z"
                if target in CHOWN_MOUNTS:
                    options += ",U"
                args.extend(("-v", f"{entry['Source']}:{target}:{options}"))
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

    async def create(self, pod: WorkerPod) -> None:
        if self.dry_run_only and not pod.dry_run:
            raise ApplicationError("Local E2E pods require dry-run requests", non_retryable=True)
        while True:
            async with self.lock:
                if slot := await self._slot(pod):
                    await self._run(
                        "volume",
                        "create",
                        "--ignore",
                        "--label",
                        f"{self.suite_label}={self.suite}",
                        pod.name,
                    )
                    break
            activity.heartbeat()
            await asyncio.sleep(POLL_SECONDS)
        common = {"GIT_REPO_BASEPATH": WORKSPACE}
        if pod.dry_run:
            common.update(DRY_RUN="true", JIRA_DRY_RUN="true")
        if self.dry_run_only:
            common.update(MOCK_JIRA="true", ALLOW_RHEL_CONFIG_OVERRIDE="true")
        await self._container(
            pod,
            slot,
            "gateway",
            self.gateway,
            GATEWAY_MOUNTS,
            self.config.gateway_env
            | common
            | {
                "MCP_BIND_HOST": "127.0.0.1",
                "SSE_PORT": "8000",
                "GIT_CONFIG_GLOBAL": f"{WORKSPACE}/.mock_gitconfig",
            },
            ("python", "-m", "ymir.tools.privileged.gateway"),
        )
        await self._container(
            pod,
            slot,
            "worker",
            self.worker,
            WORKER_MOUNTS,
            _worker_env(pod, self.config, self.address, self.namespace) | common,
            self.config.command,
        )

    async def ready(self, pod: WorkerPod) -> bool:
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

    async def delete(self, pod: WorkerPod) -> None:
        async with self.lock:
            for slot in self.slots:
                state = await self._inspect("pod", slot)
                if state and (state.get("Labels") or {}).get(self.config.execution_label) == pod.name:
                    await self._run("pod", "rm", "--force", "--ignore", state["Id"])
            await self._delete_workspace(pod.name, self.suite)

    async def _delete_workspace(self, name: str, suite: str) -> None:
        volume = await self._inspect("volume", name)
        if volume and (volume.get("Labels") or {}).get(self.suite_label) == suite:
            code, _ = await self._run("volume", "rm", name, check=False)
            if code and await self._inspect("volume", name):
                raise ContainerCommandError(f"Could not remove worker workspace {name}")

    async def cleanup(self) -> None:
        """Remove resources owned by this controller before Compose removes its network."""
        for resource in ("pod", "volume"):
            _, raw = await self._run(
                resource, "ls", "-q", "--filter", f"label={self.suite_label}={self.suite}"
            )
            if ids := raw.split():
                await self._run(resource, "rm", "--force", *ids)


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


class OpenShiftPodProvisioner:
    """Run the same worker and gateway in a namespaced OpenShift pod."""

    def __init__(
        self,
        namespace: str,
        template: Path,
        address: str,
        temporal_namespace: str,
        config: WorkerPodConfig,
        *,
        api: InClusterPodAPI | None = None,
    ):
        self.api = api
        self.namespace = namespace
        self.template = template
        self.address = address
        self.temporal_namespace = temporal_namespace
        self.config = config

    async def _run(self, *args: str, data: str | None = None, check: bool = True):
        return await _command("oc", "--namespace", self.namespace, *args, data=data, check=check)

    def manifest(self, pod: WorkerPod) -> dict:
        manifest = yaml.safe_load(self.template.read_text())
        if (manifest.get("apiVersion"), manifest.get("kind")) != ("v1", "Pod"):
            raise ValueError("Worker execution template must describe a v1 Pod")
        metadata = manifest.setdefault("metadata", {})
        metadata["name"] = pod.name
        metadata["namespace"] = self.namespace
        labels = metadata.setdefault("labels", {})
        labels.update(app=f"ymir-temporal-{self.config.workflow}", **{self.config.execution_label: pod.name})
        spec = manifest["spec"]
        spec["activeDeadlineSeconds"] = self.config.deadline_seconds
        for container in spec["containers"]:
            env = {"GIT_REPO_BASEPATH": WORKSPACE}
            if container["name"] == "worker":
                container["image"] = self.config.image
                container["args"] = list(self.config.command)
                if self.config.worker_resources is not None:
                    container["resources"] = self.config.worker_resources
                env |= _worker_env(pod, self.config, self.address, self.temporal_namespace)
            elif container["name"] == "gateway":
                env |= self.config.gateway_env | {
                    "MCP_BIND_HOST": "127.0.0.1",
                    "GIT_REPO_BASEPATH": WORKSPACE,
                }
                container.setdefault("envFrom", []).extend(
                    {"secretRef": {"name": secret}} for secret in self.config.gateway_secrets
                )
            if pod.dry_run:
                env |= {"DRY_RUN": "true", "JIRA_DRY_RUN": "true"}
            container["env"] = [
                *(item for item in container.get("env", []) if item["name"] not in env),
                *({"name": key, "value": value} for key, value in env.items()),
            ]
        return manifest

    async def _inspect(self, pod: WorkerPod) -> dict | None:
        if self.api:
            state = await self.api.request("GET", pod.name)
            if state is None:
                return None
        else:
            _, raw = await self._run("get", "pod", pod.name, "-o", "json", "--ignore-not-found")
            if not raw.strip():
                return None
            state = json.loads(raw)
        if state["metadata"].get("labels", {}).get(self.config.execution_label) != pod.name:
            raise ApplicationError(f"Pod name {pod.name} belongs to another execution", non_retryable=True)
        return state

    async def create(self, pod: WorkerPod) -> None:
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

    async def ready(self, pod: WorkerPod) -> bool:
        state = await self._inspect(pod)
        if state is None or state["metadata"].get("deletionTimestamp"):
            return False
        status = state.get("status", {})
        if status.get("phase") in {"Failed", "Succeeded"}:
            raise ApplicationError(f"Execution pod {pod.name} exited", non_retryable=True)
        return any(c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", []))

    async def delete(self, pod: WorkerPod) -> None:
        if not (state := await self._inspect(pod)):
            return
        if not self.api:
            await self._run("delete", "pod", pod.name, "--ignore-not-found", "--timeout=35s")
            return
        await self.api.request("DELETE", pod.name, {"preconditions": {"uid": state["metadata"]["uid"]}})
        async with asyncio.timeout(40):
            while await self._inspect(pod):
                await asyncio.sleep(POLL_SECONDS)


class WorkerPodActivities:
    def __init__(self, provisioner: PodProvisioner):
        self.provisioner = provisioner

    @activity.defn
    async def provision_pod(self, pod: WorkerPod) -> None:
        activity.heartbeat()
        await self.provisioner.create(pod)
        while not await self.provisioner.ready(pod):
            activity.heartbeat()
            await asyncio.sleep(POLL_SECONDS)

    @activity.defn
    async def watch_pod(self, pod: WorkerPod) -> None:
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
    async def delete_pod(self, pod: WorkerPod) -> None:
        await self.provisioner.delete(pod)


async def run_in_worker_pod(
    pod: WorkerPod,
    run_on_queue: Callable[[str], Coroutine[Any, Any, Result]],
    *,
    activities: type[WorkerPodActivities] = WorkerPodActivities,
    run_timeout: timedelta,
    cleanup_retry_policy: RetryPolicy,
    cleanup_error_type: str = "WorkerPodCleanupFailed",
) -> Result:
    """Run workflow steps on a pod's queue and confirm cleanup on every exit."""
    try:
        await workflow.execute_activity_method(
            activities.provision_pod,
            pod,
            schedule_to_close_timeout=timedelta(minutes=6),
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=75),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        watch = workflow.start_activity_method(
            activities.watch_pod,
            pod,
            start_to_close_timeout=run_timeout,
            heartbeat_timeout=timedelta(seconds=75),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        run = asyncio.create_task(run_on_queue(pod.task_queue))
        try:
            done, _ = await workflow.wait([run, watch], return_when=asyncio.FIRST_COMPLETED)
            if run in done:
                return await run
            await watch
            raise ApplicationError("Execution pod monitor exited before work finished", non_retryable=True)
        finally:
            run.cancel()
            watch.cancel()
            await asyncio.gather(run, watch, return_exceptions=True)
    finally:
        try:
            await workflow.execute_activity_method(
                activities.delete_pod,
                pod,
                start_to_close_timeout=timedelta(minutes=5),
                schedule_to_close_timeout=timedelta(minutes=15),
                retry_policy=cleanup_retry_policy,
            )
        except FailureError as error:
            raise ApplicationError(
                f"Could not confirm deletion of {pod.name}", type=cleanup_error_type, non_retryable=True
            ) from error
