"""Konflux (Tekton) build backend — a COPR-alternative build-validation tool.

Submits a component-backed RPM build PipelineRun to the Konflux API, polls it to
completion, and reports a :class:`BuildResult` with the same shape the COPR tool
returns, so :func:`ymir.agents.build_agent.run_build` stays backend-agnostic
(it dispatches purely by tool name). Selected via ``BUILD_BACKEND=konflux`` in
the gateway.

Unlike COPR (which builds a local SRPM), Konflux builds from a git ref, so the
caller must have committed and pushed the fork branch BEFORE invoking this tool
(see konflux-build-support-plan.md §1).
"""

import asyncio
import gzip
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from shutil import rmtree
from urllib.parse import parse_qs, urlsplit

import requests
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, ConfigDict, Field
from requests.adapters import HTTPAdapter, Retry

from ymir.common.models import BuildResult
from ymir.tools.base import CloneableTool as Tool
from ymir.tools.base import make_additional_context, tool_error_context
from ymir.tools.constants import YMIR_USER_AGENT
from ymir.tools.errors import ToolErrorWithContext

logger = logging.getLogger(__name__)

# Build/poll budget (mirrors COPR's values so workflow timeouts line up).
KONFLUX_BUILD_TIMEOUT = 3 * 60 * 60  # seconds
KONFLUX_TIMEOUT_GRACE_PERIOD = 60  # seconds
KONFLUX_POLLING_INTERVAL = 10  # seconds

# (connect, read) timeout for every HTTP call; without it a silent connection
# would hang forever (Retry only engages once a response/conn error arrives).
DEFAULT_TIMEOUT = (20, 60)

# Tekton PipelineRun status reasons.
# https://github.com/tektoncd/pipeline/blob/main/pkg/apis/pipeline/v1/pipelinerun_types.go
RUNNING_STATES = frozenset(
    {
        "Started",
        "Running",
        "PipelineRunPending",
        "PipelineRunStopping",
        "ResolvingPipelineRef",
        "ResolvingTaskRef",
        "CancelledRunningFinally",
        "StoppedRunningFinally",
    }
)
SUCCESSFUL_STATES = frozenset({"Succeeded", "Completed"})

# Single-arch scratch validation (mirrors COPR building one arch for speed).
DEFAULT_BUILD_ARCHITECTURES = ["x86_64"]
DEFAULT_BUILD_PLATFORMS = ["linux-mxlarge/amd64"]

# Pipeline definition to resolve the build from.
PIPELINE_PATH_IN_REPO = "pipeline/build-rpm-package.yaml"
OCI_ARTIFACT_EXPIRES_AFTER = "1d"

# Fixed build target (see konflux-release-data ymir-tenant/builds): one generic
# Component whose build-service-provisioned ServiceAccount carries the
# appstudio-pipelines-scc the pipeline pods require, and whose image repo
# receives the scratch build. git-url + revision are set per run, so this single
# Component backs every package. Overridable via env for other deployments.
DEFAULT_NAMESPACE = "ymir-tenant"
DEFAULT_SERVICE_ACCOUNT = "build-pipeline-ymir-scratch-build"
DEFAULT_IMAGE_REPO = "quay.io/redhat-user-workloads/ymir-tenant/ymir-scratch-build"

# Quay image tags allow [A-Za-z0-9_.-]; anything else is replaced.
_INVALID_TAG_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


class KonfluxConfig(BaseModel):
    """Runtime configuration read from the environment at call time.

    Read fresh per build so a rotated token (mounted file) is always current.
    """

    api_url: str
    kubearchive_url: str | None
    pipeline_url: str
    token: str
    gitlab_token: str
    namespace: str
    service_account: str
    container_image: str


def _load_config() -> KonfluxConfig:
    """Assemble :class:`KonfluxConfig` from env, raising on missing essentials."""
    missing = []

    def required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            missing.append(name)
        return value

    api_url = required("KONFLUX_API_URL").rstrip("/")
    pipeline_url = required("KONFLUX_PIPELINE_URL")
    gitlab_token = required("GITLAB_TOKEN")

    # The SA token may be supplied either as a file (KONFLUX_TOKEN_FILE, read
    # fresh per build so a rotated mounted file is always current) or inline via
    # KONFLUX_TOKEN. The file takes precedence when both are set.
    token_file = os.environ.get("KONFLUX_TOKEN_FILE", "").strip()
    inline_token = os.environ.get("KONFLUX_TOKEN", "").strip()
    token = ""
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError as e:
            raise ToolErrorWithContext(
                "Failed to read Konflux SA token file",
                cause=e,
                additional_context=make_additional_context(token_file=token_file),
            ) from e
        if not token:
            raise ToolErrorWithContext(
                "Konflux SA token file is empty",
                additional_context=make_additional_context(token_file=token_file),
            )
    elif inline_token:
        token = inline_token
    else:
        missing.append("KONFLUX_TOKEN_FILE or KONFLUX_TOKEN")

    if missing:
        raise ToolErrorWithContext(
            "Konflux backend is not fully configured",
            additional_context=make_additional_context(missing=", ".join(sorted(missing))),
        )

    kubearchive_url = os.environ.get("KUBEARCHIVE_API_URL", "").strip().rstrip("/") or None
    namespace = os.environ.get("KONFLUX_NAMESPACE", "").strip() or DEFAULT_NAMESPACE
    service_account = os.environ.get("KONFLUX_SERVICE_ACCOUNT", "").strip() or DEFAULT_SERVICE_ACCOUNT
    container_image = os.environ.get("KONFLUX_IMAGE_REPO", "").strip().rstrip("/") or DEFAULT_IMAGE_REPO
    return KonfluxConfig(
        api_url=api_url,
        kubearchive_url=kubearchive_url,
        pipeline_url=pipeline_url,
        token=token,
        gitlab_token=gitlab_token,
        namespace=namespace,
        service_account=service_account,
        container_image=container_image,
    )


def _build_session() -> requests.Session:
    """A requests Session with retries on transient/rate-limit responses."""
    session = requests.Session()
    retries = Retry(
        total=None,
        connect=10,
        read=10,
        other=10,
        status=10,
        status_forcelist=[429, 500, 502, 503, 504],
        backoff_factor=1,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    return session


def _sanitize_tag(value: str) -> str:
    return _INVALID_TAG_CHARS.sub("-", value)


def _pipelinerun_status(data: dict | None) -> str:
    """Tekton status reason, or ``Unknown`` when not yet reported."""
    if not data:
        return "Unknown"
    try:
        return data["status"]["conditions"][0]["reason"]
    except (KeyError, IndexError, TypeError):
        return "Unknown"


def _pipelinerun_message(data: dict | None) -> str:
    if not data:
        return ""
    try:
        return data["status"]["conditions"][0].get("message", "")
    except (KeyError, IndexError, TypeError):
        return ""


class KonfluxClient:
    """Thin requests wrapper around the Konflux/Tekton + Kubearchive APIs."""

    def __init__(self, config: KonfluxConfig, namespace: str) -> None:
        self._config = config
        self._namespace = namespace
        self._session = _build_session()
        self._headers = {
            "Authorization": f"Bearer {config.token}",
            "User-Agent": YMIR_USER_AGENT,
        }

    @property
    def namespace(self) -> str:
        return self._namespace

    def _tekton_base(self, api_url: str) -> str:
        return f"{api_url}/apis/tekton.dev/v1/namespaces/{self.namespace}"

    def create_secret(self, body: dict) -> str:
        url = f"{self._config.api_url}/api/v1/namespaces/{self.namespace}/secrets"
        response = self._session.post(url, headers=self._headers, json=body, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.json()["metadata"]["name"]

    def delete_secret(self, name: str) -> None:
        url = f"{self._config.api_url}/api/v1/namespaces/{self.namespace}/secrets/{name}"
        response = self._session.delete(url, headers=self._headers, timeout=DEFAULT_TIMEOUT)
        if response.status_code not in (200, 202, 404):
            response.raise_for_status()

    def create_pipelinerun(self, body: dict) -> str:
        url = f"{self._tekton_base(self._config.api_url)}/pipelineruns"
        response = self._session.post(url, headers=self._headers, json=body, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.json()["metadata"]["name"]

    def delete_pipelinerun(self, name: str) -> None:
        url = f"{self._tekton_base(self._config.api_url)}/pipelineruns/{name}"
        response = self._session.delete(url, headers=self._headers, timeout=DEFAULT_TIMEOUT)
        if response.status_code not in (200, 202, 404):
            response.raise_for_status()

    def get_pipelinerun(self, name: str) -> dict | None:
        """Fetch a PipelineRun, falling back to Kubearchive once GC'd (404)."""
        url = f"{self._tekton_base(self._config.api_url)}/pipelineruns/{name}"
        response = self._session.get(url, headers=self._headers, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 404 and self._config.kubearchive_url:
            url = f"{self._tekton_base(self._config.kubearchive_url)}/pipelineruns/{name}"
            response = self._session.get(url, headers=self._headers, timeout=DEFAULT_TIMEOUT)
        if response.status_code != 200:
            return None
        return response.json()

    def get_taskrun(self, name: str) -> dict | None:
        url = f"{self._tekton_base(self._config.api_url)}/taskruns/{name}"
        response = self._session.get(url, headers=self._headers, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 404 and self._config.kubearchive_url:
            url = f"{self._tekton_base(self._config.kubearchive_url)}/taskruns/{name}"
            response = self._session.get(url, headers=self._headers, timeout=DEFAULT_TIMEOUT)
        if response.status_code != 200:
            return None
        return response.json()


def _git_auth_secret_body(git_url: str, gitlab_token: str) -> dict:
    """basic-auth SCM secret the oci-ta clone task consumes (osci rhel-secret.j2)."""
    parts = urlsplit(git_url)
    host = parts.hostname or ""
    repo_path = parts.path.lstrip("/")
    if repo_path.endswith(".git"):
        repo_path = repo_path[: -len(".git")]
    repository_url = f"https://{host}/{repo_path}"
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "generateName": "gitlab-secret-",
            "labels": {
                "appstudio.redhat.com/credentials": "scm",
                "appstudio.redhat.com/scm.host": host,
            },
            "annotations": {
                "appstudio.redhat.com/scm.repository": repository_url,
            },
        },
        "type": "kubernetes.io/basic-auth",
        "stringData": {
            "username": "gitlab-ci-token",
            "password": gitlab_token,
        },
    }


def _pipelinerun_body(
    *,
    config: KonfluxConfig,
    namespace: str,
    service_account: str,
    run_name: str,
    package_name: str,
    git_url: str,
    revision: str,
    target_branch: str,
    specfile: str | None,
    ocistorage: str,
    pipeline_revision: str,
    secret_name: str,
) -> dict:
    """Build PipelineRun for Ymir's scratch-build Component (osci build_pipelinerun.j2, trimmed).

    Runs under the ``build-pipeline-ymir-scratch-build`` ServiceAccount in
    ymir-tenant -- that SA is provisioned by Konflux build-service with the
    ``appstudio-pipelines-scc`` the pipeline pods require (a bare bot SA is not,
    which is why a standalone run fails at pod admission). ociStorage is that
    Component's own image repo (scratch tag, short TTL). git-url + revision come
    from the caller, so one Component backs every package.

    We deliberately OMIT the ``appstudio.openshift.io/application``/``component``
    labels so integration-service does not create a Snapshot/Release and trigger
    the brew/koji import -- Ymir only needs the build itself to go green.
    koji-target is DEFAULT because the pipeline derives the real target from the
    branch (see plan §1 step 4).
    """
    return {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "name": run_name,
            "namespace": namespace,
            "annotations": {
                "build.appstudio.openshift.io/repo": git_url,
                "build.appstudio.redhat.com/commit_sha": revision,
                "build.appstudio.redhat.com/target_branch": target_branch,
                "pipelinesascode.tekton.dev/max-keep-runs": "3",
            },
            "labels": {
                "pipelines.appstudio.openshift.io/type": "build",
            },
        },
        "spec": {
            "params": [
                {"name": "package-name", "value": package_name},
                {"name": "git-url", "value": git_url},
                {"name": "ociStorage", "value": ocistorage},
                {"name": "revision", "value": revision},
                {"name": "target-branch", "value": target_branch},
                {"name": "SINGLE_COMPONENT", "value": "true"},
                {"name": "hermetic", "value": "true"},
                {"name": "koji-target", "value": "DEFAULT"},
                {"name": "specfile", "value": specfile or "null"},
                {"name": "build-platforms", "value": DEFAULT_BUILD_PLATFORMS},
                {"name": "build-architectures", "value": DEFAULT_BUILD_ARCHITECTURES},
                {"name": "self-ref-url", "value": config.pipeline_url},
                {"name": "self-ref-revision", "value": pipeline_revision},
                {"name": "ociArtifactExpiresAfter", "value": OCI_ARTIFACT_EXPIRES_AFTER},
            ],
            "pipelineRef": {
                "resolver": "git",
                "params": [
                    {"name": "url", "value": config.pipeline_url},
                    {"name": "revision", "value": pipeline_revision},
                    {"name": "pathInRepo", "value": PIPELINE_PATH_IN_REPO},
                ],
            },
            "timeouts": {"pipeline": "0", "tasks": "0", "finally": "0"},
            "taskRunTemplate": {"serviceAccountName": service_account},
            "workspaces": [
                {"name": "git-auth", "secret": {"secretName": secret_name}},
            ],
        },
    }


async def _resolve_pipeline_revision(pipeline_url: str) -> str:
    """Pin the pipeline definition to an immutable SHA via ``git ls-remote``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        pipeline_url,
        "refs/heads/main",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0 or not stdout.strip():
        raise ToolErrorWithContext(
            "Failed to resolve the pipeline revision",
            additional_context=make_additional_context(
                pipeline_url=pipeline_url,
                stderr=stderr.decode(errors="replace"),
            ),
        )
    return stdout.split()[0].decode()


def _collect_failure_log_urls(client: KonfluxClient, pipelinerun: dict, config: KonfluxConfig) -> list[str]:
    """Authenticated Kubearchive pod-log URLs for non-successful taskruns."""
    if not config.kubearchive_url:
        return []
    urls: list[str] = []
    child_refs = (pipelinerun.get("status") or {}).get("childReferences", []) or []
    for child in child_refs:
        name = child.get("name")
        if not name:
            continue
        taskrun = client.get_taskrun(name)
        if not taskrun:
            continue
        status = _pipelinerun_status(taskrun)
        if status in SUCCESSFUL_STATES:
            continue
        task_status = taskrun.get("status") or {}
        pod_name = task_status.get("podName")
        if not pod_name:
            continue
        steps = [s["container"] for s in task_status.get("steps", []) if "container" in s]
        urls.extend(
            f"{config.kubearchive_url}/api/v1/namespaces/{client.namespace}"
            f"/pods/{pod_name}/log?container={container}"
            for container in steps
        )
    return urls


class BuildPackageToolInput(BaseModel):
    """Konflux build inputs.

    ``extra='allow'`` so the COPR-only fields in the shared BuildInputSchema
    (srpm_path, dist_git_branch) are tolerated when build_agent dumps the whole
    model. This must be ``allow`` rather than ``ignore``: only ``allow`` makes
    Pydantic emit ``additionalProperties: true`` in the advertised JSON schema,
    and beeai's MCP client rebuilds the tool's input model from that schema with
    ``extra='forbid'`` unless ``additionalProperties`` is truthy. With ``ignore``
    the extra COPR fields would be rejected client-side as "Tool input
    validation error" before the build ever reaches the gateway.
    """

    model_config = ConfigDict(extra="allow")

    git_url: str = Field(description="Clonable URL of the fork holding the pushed commit")
    revision: str = Field(description="Pushed commit SHA to build")
    package_name: str = Field(description="RPM package name")
    target_branch: str = Field(description="dist-git branch the build targets")
    specfile: str | None = Field(
        default=None, description="Spec file name when it differs from <package>.spec"
    )
    jira_issue: str | None = Field(default=None, description="Jira issue key, for logging only")


class BuildPackageToolOutput(JSONToolOutput[BuildResult]):
    pass


class KonfluxBuildTool(Tool[BuildPackageToolInput, ToolRunOptions, BuildPackageToolOutput]):
    name = "build_package"
    # Must exceed the polling budget (KONFLUX_BUILD_TIMEOUT + grace).
    timeout = KONFLUX_BUILD_TIMEOUT + 2 * KONFLUX_TIMEOUT_GRACE_PERIOD
    description = """
    Builds the specified package revision in Konflux (Tekton PipelineRun).
    """
    input_schema = BuildPackageToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "konflux", self.name], creator=self)

    async def _run(
        self,
        tool_input: BuildPackageToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> BuildPackageToolOutput:
        config = _load_config()
        client = KonfluxClient(config, config.namespace)

        pipeline_revision = await _resolve_pipeline_revision(config.pipeline_url)

        short_sha = _sanitize_tag(tool_input.revision[:12]) or "rev"
        run_id = uuid.uuid4().hex[:8]
        run_name = f"ymir-build-{short_sha}-{run_id}"
        tag = _sanitize_tag(f"{tool_input.package_name}-{short_sha}-{run_id}")
        ocistorage = f"{config.container_image}:{tag}"

        with tool_error_context(
            "Failed to create the Konflux git-auth secret",
            package=tool_input.package_name,
            git_url=tool_input.git_url,
        ):
            secret_body = _git_auth_secret_body(tool_input.git_url, config.gitlab_token)
            secret_name = await asyncio.to_thread(client.create_secret, secret_body)

        try:
            body = _pipelinerun_body(
                config=config,
                namespace=config.namespace,
                service_account=config.service_account,
                run_name=run_name,
                package_name=tool_input.package_name,
                git_url=tool_input.git_url,
                revision=tool_input.revision,
                target_branch=tool_input.target_branch,
                specfile=tool_input.specfile,
                ocistorage=ocistorage,
                pipeline_revision=pipeline_revision,
                secret_name=secret_name,
            )
            with tool_error_context(
                "Failed to submit the Konflux PipelineRun",
                package=tool_input.package_name,
                run_name=run_name,
            ):
                submitted_name = await asyncio.to_thread(client.create_pipelinerun, body)
            logger.info(
                "%s: Konflux build submitted: %s (ociStorage %s)",
                tool_input.jira_issue or tool_input.package_name,
                submitted_name,
                ocistorage,
            )
            return await self._poll_to_completion(client, submitted_name, config)
        finally:
            try:
                await asyncio.to_thread(client.delete_secret, secret_name)
            except Exception as e:
                logger.warning("Failed to delete Konflux git-auth secret %s: %s", secret_name, e)

    async def _poll_to_completion(
        self,
        client: KonfluxClient,
        run_name: str,
        config: KonfluxConfig,
    ) -> BuildPackageToolOutput:
        start = time.monotonic()
        while time.monotonic() - start < KONFLUX_BUILD_TIMEOUT + KONFLUX_TIMEOUT_GRACE_PERIOD:
            pipelinerun = await asyncio.to_thread(client.get_pipelinerun, run_name)
            status = _pipelinerun_status(pipelinerun)
            if status in SUCCESSFUL_STATES:
                logger.info("Konflux build %s succeeded", run_name)
                return BuildPackageToolOutput(result=BuildResult(success=True))
            if status in RUNNING_STATES or status == "Unknown":
                await asyncio.sleep(KONFLUX_POLLING_INTERVAL)
                continue
            message = _pipelinerun_message(pipelinerun) or status
            logger.info("Konflux build %s failed: %s (%s)", run_name, status, message)
            log_urls = await asyncio.to_thread(_collect_failure_log_urls, client, pipelinerun or {}, config)
            return BuildPackageToolOutput(
                result=BuildResult(
                    success=False,
                    error_message=f"PipelineRun {run_name} finished as {status}: {message}",
                    artifacts_urls=log_urls or None,
                )
            )

        message = f"Reached timeout for Konflux build {run_name}"
        logger.info(message)
        return BuildPackageToolOutput(
            result=BuildResult(success=False, is_timeout=True, error_message=message)
        )


class DownloadArtifactsToolInput(BaseModel):
    artifacts_urls: list[str] = Field(description="URLs to build artifacts (logs)")


class DownloadArtifactsResult(BaseModel):
    target_path: Path = Field(description="Location of downloaded files")


class DownloadArtifactsToolOutput(JSONToolOutput[DownloadArtifactsResult]):
    def get_text_content(self) -> str:
        return f"target_path: {self.result.target_path}"


class KonfluxDownloadArtifactsTool(
    Tool[DownloadArtifactsToolInput, ToolRunOptions, DownloadArtifactsToolOutput]
):
    name = "download_artifacts"
    timeout = 120
    description = """
    Downloads Konflux build artifacts (task logs) to a temporary location.
    Gzipped logs are decompressed automatically.
    """
    input_schema = DownloadArtifactsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "konflux", self.name], creator=self)

    async def _run(
        self,
        tool_input: DownloadArtifactsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> DownloadArtifactsToolOutput:
        # Kubearchive pod-log endpoints are authenticated (unlike COPR's public URLs).
        config = _load_config()
        headers = {"Authorization": f"Bearer {config.token}", "User-Agent": YMIR_USER_AGENT}
        session = _build_session()
        target_path = Path(tempfile.mkdtemp())
        try:
            for url in tool_input.artifacts_urls:
                logger.info("Downloading Konflux build artifact from: %s", url)
                with tool_error_context("Failed to download build artifact", artifacts_url=url):
                    content = await asyncio.to_thread(self._download, session, url, headers)
                (target_path / self._filename_for(url)).write_bytes(content)
        except Exception:
            rmtree(target_path)
            raise
        return DownloadArtifactsToolOutput(result=DownloadArtifactsResult(target_path=target_path))

    @staticmethod
    def _download(session: requests.Session, url: str, headers: dict) -> bytes:
        response = session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        if response.status_code >= 400:
            raise ToolError(f"{response.status_code} {response.reason}")
        content = response.content
        if content.startswith(b"\x1f\x8b"):
            content = gzip.decompress(content)
        return content

    @staticmethod
    def _filename_for(url: str) -> str:
        parts = urlsplit(url)
        path_segments = [s for s in parts.path.split("/") if s]
        pod = path_segments[path_segments.index("pods") + 1] if "pods" in path_segments else ""
        container = (parse_qs(parts.query).get("container") or [""])[0]
        name = f"{pod}__{container}.log" if pod and container else Path(parts.path).name or "artifact.log"
        return _sanitize_tag(name)
