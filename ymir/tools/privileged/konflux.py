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
import rpm
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, ConfigDict, Field
from requests.adapters import HTTPAdapter, Retry

from ymir.common.base_utils import is_cs_branch
from ymir.common.config import load_rhel_config
from ymir.common.models import BuildResult
from ymir.common.version_utils import (
    construct_internal_branch_name,
    get_maintenance_rhel_branch,
    parse_branch_name,
    parse_rhel_version,
)
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
SUCCESSFUL_STATES = frozenset({"Succeeded", "Completed"})
PIPELINERUN_TIMEOUT_REASON = "PipelineRunTimeout"

# Single-arch scratch validation. Use this preference order while respecting
# the SRPM's ExclusiveArch and ExcludeArch tags.
BUILD_ARCH_PREFERENCE = ("x86_64", "ppc64le", "s390x", "aarch64")
BUILD_PLATFORMS = {
    "x86_64": "linux-mxlarge/amd64",
    "ppc64le": "linux/ppc64le",
    "s390x": "linux/s390x",
    "aarch64": "linux/arm64",
}

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


def _select_build_architecture(srpm_path: Path) -> str:
    """Choose one supported architecture allowed by the generated SRPM."""
    transaction = rpm.TransactionSet()
    transaction.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)
    with srpm_path.open("rb") as srpm:
        header = transaction.hdrFromFdno(srpm.fileno())

    excluded = set(header[rpm.RPMTAG_EXCLUDEARCH] or ())
    exclusive = set(header[rpm.RPMTAG_EXCLUSIVEARCH] or ())
    for architecture in BUILD_ARCH_PREFERENCE:
        if architecture not in excluded and (not exclusive or architecture in exclusive):
            return architecture
    raise ValueError("No Konflux build architecture is usable for this SRPM")


def _build_platform(architecture: str) -> str:
    """Return the cluster's host label for an RPM architecture."""
    environment_key = f"KONFLUX_BUILD_PLATFORM_{architecture.upper()}"
    return os.environ.get(environment_key, "").strip() or BUILD_PLATFORMS[architecture]


# CentOS Stream dist-git branches cannot be built on this RHEL/Brew-backed Konflux:
# the pipeline's get-build-target step resolves ``<branch>-candidate`` against Brew,
# which only carries RHEL targets (e.g. ``c8s-candidate`` does not exist, so the build
# fails with "No such tagInfo: 'null'"). Konflux cannot build CentOS Stream at all, so
# remap each CentOS Stream branch to the real RHEL branch Brew can build and build the
# source against that target instead. The stream data comes from ``rhel-config.json``
# (no versions are hard-coded here):
#   * maintenance-phase majors (a Z-stream but no Y-stream) build against the current
#     Z-stream branch -- reusing the shared ``get_maintenance_rhel_branch`` helper;
#   * active majors build against the current Y-stream.
async def _map_centos_build_target(target_branch: str) -> str:
    """Remap a CentOS Stream branch to the RHEL branch Konflux can actually build.

    Non-CentOS branches (e.g. ``rhel-10.2``) are returned unchanged.
    """
    if not is_cs_branch(target_branch):
        return target_branch

    # Maintenance phase (Z-stream only): build against the current Z-stream branch.
    maintenance_branch = await get_maintenance_rhel_branch(target_branch)
    if maintenance_branch is not None:
        logger.info(
            "CentOS Stream target %s is in maintenance phase; remapping to current "
            "Z-stream branch %s for the Konflux build",
            target_branch,
            maintenance_branch,
        )
        return maintenance_branch

    # Active phase: build against the current Y-stream. The ``current_y_streams``
    # value is a fixVersion-style string (e.g. ``rhel-9.8``); normalize it to the
    # internal build-target branch form, which carries the ``.0`` suffix for
    # RHEL < 10 (``rhel-9.8.0``) that Brew's ``<branch>-candidate`` resolves
    # against. RHEL >= 10 has no ``.0`` suffix (``rhel-10.2``).
    if parsed := parse_branch_name(target_branch):
        major, _ = parsed
        config = await load_rhel_config()
        y_stream = config.get("current_y_streams", {}).get(major)
        if y_stream and (y_parsed := parse_rhel_version(y_stream)):
            y_major, y_minor, _ = y_parsed
            build_branch = construct_internal_branch_name(y_major, y_minor)
            logger.info(
                "CentOS Stream target %s cannot be built on Konflux; remapping to "
                "current RHEL Y-stream branch %s",
                target_branch,
                build_branch,
            )
            return build_branch

    return target_branch


def _pipelinerun_status(data: dict | None) -> str:
    """Tekton status reason, or ``Unknown`` when not yet reported."""
    if not data:
        return "Unknown"
    try:
        return data["status"]["conditions"][0]["reason"]
    except (KeyError, IndexError, TypeError):
        return "Unknown"


def _pipelinerun_is_done(data: dict | None) -> bool:
    """Only a True or False Succeeded condition is terminal in Tekton."""
    try:
        return data["status"]["conditions"][0]["status"] in ("True", "False")
    except (KeyError, IndexError, TypeError):
        return False


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

    def cancel_pipelinerun(self, name: str) -> bool:
        """Ask Tekton to stop TaskRuns; return False if the run is already absent."""
        url = f"{self._tekton_base(self._config.api_url)}/pipelineruns/{name}"
        response = self._session.patch(
            url,
            headers={**self._headers, "Content-Type": "application/merge-patch+json"},
            json={"spec": {"status": "Cancelled"}},
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

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
            logger.warning(
                "Failed to get Konflux PipelineRun %s: HTTP %s %s",
                name,
                response.status_code,
                response.reason,
            )
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
    build_architecture: str,
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
                {"name": "build-platforms", "value": [_build_platform(build_architecture)]},
                {"name": "build-architectures", "value": [build_architecture]},
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
            "timeouts": {"pipeline": f"{KONFLUX_BUILD_TIMEOUT}s"},
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
    """Authenticated LIVE pod-log URLs for non-successful taskruns.

    Uses the live cluster API (``config.api_url``): a just-failed build's pods
    are still on the cluster and only move to Kubearchive after GC, so a
    Kubearchive URL 404s right after failure (exactly when diagnosis runs). The
    downloader falls back to Kubearchive for any pod already GC'd by the time
    logs are fetched.
    """
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
            f"{config.api_url}/api/v1/namespaces/{client.namespace}/pods/{pod_name}/log?container={container}"
            for container in steps
        )
    return urls


class BuildPackageToolInput(BaseModel):
    """Konflux build inputs.

    ``extra='allow'`` so the COPR-only ``dist_git_branch`` field in the shared
    BuildInputSchema is tolerated when build_agent dumps the whole model.
    This must be ``allow`` rather than ``ignore``: only ``allow`` makes
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
    srpm_path: Path | None = Field(
        default=None, description="Generated SRPM used to choose a supported build architecture"
    )
    jira_issue: str | None = Field(default=None, description="Jira issue key, for logging only")


class BuildPackageToolOutput(JSONToolOutput[BuildResult]):
    pass


class KonfluxBuildTool(Tool[BuildPackageToolInput, ToolRunOptions, BuildPackageToolOutput]):
    name = "build_package"
    # Leave room after polling for Tekton cancellation and Secret cleanup.
    timeout = KONFLUX_BUILD_TIMEOUT + 5 * KONFLUX_TIMEOUT_GRACE_PERIOD
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

        build_architecture = BUILD_ARCH_PREFERENCE[0]
        if tool_input.srpm_path is not None:
            with tool_error_context(
                "Failed to choose a Konflux build architecture",
                srpm_path=tool_input.srpm_path,
            ):
                build_architecture = await asyncio.to_thread(_select_build_architecture, tool_input.srpm_path)
        logger.info("Konflux build architecture for %s: %s", tool_input.package_name, build_architecture)

        pipeline_revision = await _resolve_pipeline_revision(config.pipeline_url)

        short_sha = _sanitize_tag(tool_input.revision[:12]) or "rev"
        run_id = uuid.uuid4().hex[:8]
        run_name = f"ymir-build-{short_sha}-{run_id}"
        tag = _sanitize_tag(f"{tool_input.package_name}-{short_sha}-{run_id}")
        ocistorage = f"{config.container_image}:{tag}"

        secret_name: str | None = None
        secret_creation: asyncio.Task[str] | None = None
        run_to_cleanup: str | None = None
        submission: asyncio.Task[str] | None = None
        run_terminal = False
        try:
            with tool_error_context(
                "Failed to create the Konflux git-auth secret",
                package=tool_input.package_name,
                git_url=tool_input.git_url,
            ):
                secret_body = _git_auth_secret_body(tool_input.git_url, config.gitlab_token)
                # The API request may complete after cancellation. Keep the
                # task so cleanup can wait for its returned Secret name.
                secret_creation = asyncio.create_task(asyncio.to_thread(client.create_secret, secret_body))
                secret_name = await asyncio.shield(secret_creation)

            build_target_branch = await _map_centos_build_target(tool_input.target_branch)
            body = _pipelinerun_body(
                config=config,
                namespace=config.namespace,
                service_account=config.service_account,
                run_name=run_name,
                package_name=tool_input.package_name,
                git_url=tool_input.git_url,
                revision=tool_input.revision,
                target_branch=build_target_branch,
                specfile=tool_input.specfile,
                build_architecture=build_architecture,
                ocistorage=ocistorage,
                pipeline_revision=pipeline_revision,
                secret_name=secret_name,
            )
            with tool_error_context(
                "Failed to submit the Konflux PipelineRun",
                package=tool_input.package_name,
                run_name=run_name,
            ):
                # The request can finish after our task is cancelled. Keep its
                # name and await the shielded request before cleanup in that case.
                run_to_cleanup = run_name
                submission = asyncio.create_task(asyncio.to_thread(client.create_pipelinerun, body))
                submitted_name = await asyncio.shield(submission)
                run_to_cleanup = submitted_name
            logger.info(
                "%s: Konflux build submitted: %s (ociStorage %s)",
                tool_input.jira_issue or tool_input.package_name,
                submitted_name,
                ocistorage,
            )
            result, run_terminal = await self._poll_to_completion(client, submitted_name, config)
            return result
        finally:
            if secret_creation is not None and secret_name is None:
                try:
                    secret_name = await asyncio.shield(secret_creation)
                except Exception as error:
                    logger.warning("Konflux git-auth Secret creation did not return a name: %s", error)
            if submission is not None:
                try:
                    run_to_cleanup = await asyncio.shield(submission)
                except Exception as error:
                    # The request may have reached the API before its response
                    # failed. The generated name still lets us check/cancel it.
                    logger.warning(
                        "Konflux PipelineRun submission did not return a name for %s: %s",
                        run_name,
                        error,
                    )
            if secret_name is not None:
                await asyncio.shield(
                    self._cleanup_git_auth(client, secret_name, run_to_cleanup, run_terminal)
                )

    async def _cleanup_git_auth(
        self,
        client: KonfluxClient,
        secret_name: str,
        run_name: str | None,
        run_terminal: bool,
    ) -> None:
        if run_name and not run_terminal:
            run_terminal = await self._cancel_and_wait(client, run_name)
        if not run_name or run_terminal:
            try:
                await asyncio.to_thread(client.delete_secret, secret_name)
            except Exception as e:
                logger.warning("Failed to delete Konflux git-auth secret %s: %s", secret_name, e)
        else:
            logger.warning(
                "Keeping Konflux git-auth secret %s: PipelineRun %s may still be active",
                secret_name,
                run_name,
            )

    async def _cancel_and_wait(self, client: KonfluxClient, run_name: str) -> bool:
        try:
            if not await asyncio.to_thread(client.cancel_pipelinerun, run_name):
                return True  # The live PipelineRun is already gone.
        except Exception as e:
            logger.warning("Failed to cancel Konflux PipelineRun %s: %s", run_name, e)
            try:
                return _pipelinerun_is_done(await asyncio.to_thread(client.get_pipelinerun, run_name))
            except Exception:
                return False

        deadline = time.monotonic() + KONFLUX_TIMEOUT_GRACE_PERIOD
        while True:
            try:
                pipelinerun = await asyncio.to_thread(client.get_pipelinerun, run_name)
            except Exception as e:
                logger.warning("Failed to confirm Konflux PipelineRun %s stopped: %s", run_name, e)
                return False
            if _pipelinerun_is_done(pipelinerun):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Timed out waiting for Konflux PipelineRun %s to stop", run_name)
                return False
            await asyncio.sleep(min(KONFLUX_POLLING_INTERVAL, remaining))

    async def _poll_to_completion(
        self,
        client: KonfluxClient,
        run_name: str,
        config: KonfluxConfig,
    ) -> tuple[BuildPackageToolOutput, bool]:
        """Return the build result and whether Tekton reached a terminal state."""
        start = time.monotonic()
        while time.monotonic() - start < KONFLUX_BUILD_TIMEOUT + KONFLUX_TIMEOUT_GRACE_PERIOD:
            pipelinerun = await asyncio.to_thread(client.get_pipelinerun, run_name)
            status = _pipelinerun_status(pipelinerun)
            if not _pipelinerun_is_done(pipelinerun):
                await asyncio.sleep(KONFLUX_POLLING_INTERVAL)
                continue
            if status in SUCCESSFUL_STATES:
                logger.info("Konflux build %s succeeded", run_name)
                return BuildPackageToolOutput(result=BuildResult(success=True)), True
            message = _pipelinerun_message(pipelinerun) or status
            logger.info("Konflux build %s failed: %s (%s)", run_name, status, message)
            log_urls = await asyncio.to_thread(_collect_failure_log_urls, client, pipelinerun or {}, config)
            return (
                BuildPackageToolOutput(
                    result=BuildResult(
                        success=False,
                        is_timeout=status == PIPELINERUN_TIMEOUT_REASON,
                        error_message=f"PipelineRun {run_name} finished as {status}: {message}",
                        artifacts_urls=log_urls or None,
                    )
                ),
                True,
            )

        message = f"Reached timeout for Konflux build {run_name}"
        logger.info(message)
        return (
            BuildPackageToolOutput(result=BuildResult(success=False, is_timeout=True, error_message=message)),
            False,
        )


class DownloadArtifactsToolInput(BaseModel):
    artifacts_urls: list[str] = Field(description="URLs to build artifacts (logs)")


class DownloadArtifactsResult(BaseModel):
    target_path: Path = Field(description="Location of downloaded files")
    artifact_paths: list[Path] = Field(
        default_factory=list, description="Full paths of the downloaded log files"
    )


class DownloadArtifactsToolOutput(JSONToolOutput[DownloadArtifactsResult]):
    def get_text_content(self) -> str:
        if self.result.artifact_paths:
            listing = "\n".join(str(path) for path in self.result.artifact_paths)
            return f"target_path: {self.result.target_path}\nDownloaded log files:\n{listing}"
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
        # Reject every caller-supplied URL before logging it or making a request.
        for url in tool_input.artifacts_urls:
            self._validate_log_url(url, config)
        headers = {"Authorization": f"Bearer {config.token}", "User-Agent": YMIR_USER_AGENT}
        session = _build_session()
        target_path = Path(tempfile.mkdtemp())
        artifact_paths: list[Path] = []
        try:
            for url in tool_input.artifacts_urls:
                logger.info("Downloading Konflux build artifact from: %s", url)
                with tool_error_context("Failed to download build artifact", artifacts_url=url):
                    content = await asyncio.to_thread(self._download, session, url, headers, config)
                artifact_file = target_path / self._filename_for(url)
                artifact_file.write_bytes(content)
                artifact_paths.append(artifact_file)
        except Exception:
            rmtree(target_path)
            raise
        return DownloadArtifactsToolOutput(
            result=DownloadArtifactsResult(target_path=target_path, artifact_paths=artifact_paths)
        )

    @staticmethod
    def _validate_log_url(url: str, config: KonfluxConfig) -> str:
        """Return the configured API base for an authenticated pod-log URL.

        Only URLs in the format produced by ``_collect_failure_log_urls`` (or
        their archive equivalents) may receive the token. Compare complete
        origins rather than string prefixes, and restrict the API resource so
        a caller cannot use this tool to read other cluster objects.
        """
        error = ToolError("Invalid Konflux pod-log URL")
        if url != url.strip() or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise error
        try:
            parts = urlsplit(url)
            allowed_bases = [base for base in (config.api_url, config.kubearchive_url) if base]
            matching_base = next(
                (
                    base
                    for base in allowed_bases
                    if (parsed := urlsplit(base)).scheme == "https"
                    and parsed.netloc
                    and parsed.netloc == parts.netloc
                    and parsed.path in ("", "/")
                    and not parsed.query
                    and not parsed.fragment
                    and parsed.username is None
                ),
                None,
            )
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError as e:
            raise error from e

        pod_log_path = (
            rf"/api/v1/namespaces/{re.escape(config.namespace)}"
            r"/pods/[a-z0-9](?:[-.a-z0-9]*[a-z0-9])?/log"
        )
        container = query.get("container", [])
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.fragment
            or matching_base is None
            or re.fullmatch(pod_log_path, parts.path) is None
            or set(query) != {"container"}
            or len(container) != 1
            or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", container[0]) is None
        ):
            raise error
        return matching_base

    @staticmethod
    def _fallback_url(url: str, config: KonfluxConfig) -> str | None:
        """Swap a pod-log URL between the live API host and Kubearchive.

        A pod is readable on the live API until GC, then only via Kubearchive;
        retrying the other host on a 404 covers both sides of that handoff.
        """
        source = KonfluxDownloadArtifactsTool._validate_log_url(url, config)
        if not config.kubearchive_url:
            return None
        destination = config.kubearchive_url if source == config.api_url else config.api_url
        parts = urlsplit(url)
        fallback = f"{destination.rstrip('/')}{parts.path}?{parts.query}"
        KonfluxDownloadArtifactsTool._validate_log_url(fallback, config)
        return fallback

    @staticmethod
    def _download(session: requests.Session, url: str, headers: dict, config: KonfluxConfig) -> bytes:
        KonfluxDownloadArtifactsTool._validate_log_url(url, config)
        response = session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
        if response.status_code == 404:
            fallback = KonfluxDownloadArtifactsTool._fallback_url(url, config)
            if fallback:
                logger.info("Konflux log 404 at %s; retrying via %s", url, fallback)
                response = session.get(
                    fallback, headers=headers, timeout=DEFAULT_TIMEOUT, allow_redirects=False
                )
        if response.status_code >= 300:
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
