import asyncio
import base64
import logging
import os
import re
import subprocess
import threading
from datetime import datetime
from functools import cache
from json import dumps as json_dumps
from pathlib import Path
from typing import Any

import requests
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolError, ToolRunOptions
from pydantic import BaseModel, Field, field_validator

from ymir.common.models import (
    TestingFarmRequest,
    TestingFarmRequestResult,
)

logger = logging.getLogger(__name__)

_REDACTED_KEYS = frozenset({"secrets", "api_key", "token", "password"})


def _redact_secrets(obj: Any) -> Any:
    """Recursively redact sensitive keys from a nested dict/list structure."""
    if isinstance(obj, dict):
        return {k: ("***" if k in _REDACTED_KEYS else _redact_secrets(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_secrets(item) for item in obj]
    return obj


@cache
def _testing_farm_url() -> str:
    return os.environ.get("TESTING_FARM_API_URL", "https://api.testing-farm.io/v0.1")


@cache
def _testing_farm_headers() -> dict[str, str]:
    token = os.environ["TESTING_FARM_API_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


_SSH_KEY_PATH = Path.home() / ".ssh" / "id_ed25519"
_ssh_key_lock = threading.Lock()


def _ensure_gateway_ssh_key() -> str:
    """Ensure the gateway has an SSH key pair and return the public key content."""
    pub_path = _SSH_KEY_PATH.with_suffix(".pub")
    with _ssh_key_lock:
        if not _SSH_KEY_PATH.exists() or not pub_path.exists():
            _SSH_KEY_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _SSH_KEY_PATH.unlink(missing_ok=True)
            pub_path.unlink(missing_ok=True)
            subprocess.run(  # noqa: S603
                ["ssh-keygen", "-t", "ed25519", "-f", str(_SSH_KEY_PATH), "-N", "", "-q"],  # noqa: S607
                check=True,
            )
            logger.info("Generated gateway SSH key pair at %s", _SSH_KEY_PATH)
    return pub_path.read_text().strip()


def _testing_farm_api_get(path: str, *, params: dict | None = None) -> Any:
    url = f"{_testing_farm_url()}/{path}"
    response = requests.get(url, headers=_testing_farm_headers(), params=params, timeout=30)
    if not response.ok:
        logger.error(
            "GET %s%s failed.\nerror:\n%s", url, f" (params={params})" if params else "", response.text
        )
    response.raise_for_status()
    return response.json()


def _testing_farm_api_post(path: str, json: dict[str, Any]) -> Any:
    url = f"{_testing_farm_url()}/{path}"
    response = requests.post(url, headers=_testing_farm_headers(), json=json, timeout=30)
    if not response.ok:
        logger.error(
            "POST to %s failed\nbody:\n%s\nerror:\n%s",
            url, json_dumps(_redact_secrets(json), indent=2), response.text
        )
    response.raise_for_status()
    return response.json()


def _testing_farm_api_delete(path: str) -> None:
    url = f"{_testing_farm_url()}/{path}"
    response = requests.delete(url, headers=_testing_farm_headers(), timeout=30)
    if not response.ok:
        logger.error("DELETE %s failed.\nerror:\n%s", url, response.text)
    response.raise_for_status()


def _parse_tf_request(response: dict[str, Any]) -> TestingFarmRequest:
    result_data = response.get("result")
    result = result_data["overall"] if result_data else TestingFarmRequestResult.UNKNOWN
    error_reason = result_data.get("summary") if result == TestingFarmRequestResult.ERROR else None

    return TestingFarmRequest(
        id=response["id"],
        url=f"{_testing_farm_url()}/requests/{response['id']}",
        state=response["state"],
        result=result,
        error_reason=error_reason,
        result_xunit_url=result_data.get("xunit_url") if result_data else None,
        created=datetime.fromisoformat(response["created"]),
        updated=datetime.fromisoformat(response["updated"]),
        test_data=response.get("test", {}),
        environments_data=response.get("environments_requested", response.get("environments", [])),
    )


# -- MCP Tools --


class GetTestingFarmRequestToolInput(BaseModel):
    request_id: str = Field(description="Testing Farm request ID")


class GetTestingFarmRequestTool(
    Tool[GetTestingFarmRequestToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "get_testing_farm_request"
    description = """
    Get a Testing Farm request by ID.
    """
    input_schema = GetTestingFarmRequestToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "testing_farm", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetTestingFarmRequestToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        logger.info("Getting Testing Farm request %s", tool_input.request_id)
        try:
            response = await asyncio.to_thread(_testing_farm_api_get, f"requests/{tool_input.request_id}")
            tf_request = _parse_tf_request(response)
        except Exception as e:
            raise ToolError(f"Failed to get Testing Farm request {tool_input.request_id}: {e}") from e

        return JSONToolOutput(result=tf_request.model_dump(mode="json"))


class ReproduceTestingFarmRequestToolInput(BaseModel):
    request_id: str = Field(description="ID of the original Testing Farm request to reproduce")
    build_nvr: str = Field(description="NVR of the build to use for reproduction")


class ReproduceTestingFarmRequestTool(
    Tool[ReproduceTestingFarmRequestToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "reproduce_testing_farm_request"
    description = """
    Reproduce a Testing Farm request with a different build NVR.
    """
    input_schema = ReproduceTestingFarmRequestToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "testing_farm", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: ReproduceTestingFarmRequestToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        request_id = tool_input.request_id
        build_nvr = tool_input.build_nvr
        logger.info("Reproducing Testing Farm request %s with build %s", request_id, build_nvr)

        if os.getenv("DRY_RUN", "False").lower() == "true":
            return JSONToolOutput(
                result={
                    "id": f"dry-run-{request_id}",
                    "message": f"Dry run: would reproduce {request_id} with build {build_nvr}",
                }
            )

        try:
            # Fetch the original request
            original_response = await asyncio.to_thread(_testing_farm_api_get, f"requests/{request_id}")
            original = _parse_tf_request(original_response)

            # Build new environments with the replacement build
            def create_new_environment(env: dict) -> dict:
                new_env = {
                    "arch": env["arch"],
                    "os": env["os"],
                    "tmt": {
                        "context": {
                            k: v for k, v in env["tmt"]["context"].items() if not k.startswith("newa_")
                        }
                    },
                }

                builds_var = env.get("variables", {}).get("BUILDS")
                if builds_var is not None:
                    new_env["variables"] = env["variables"] | {"BUILDS": build_nvr}
                    return new_env

                new_env["variables"] = env.get("variables", {})

                artifacts = env.get("artifacts")
                if artifacts and len(artifacts) == 1:
                    new_env["artifacts"] = [{"id": build_nvr, "type": "redhat-brew-build", "order": 40}]
                    return new_env

                raise ToolError(
                    "Cannot reproduce Testing Farm request: "
                    "cannot determine how to replace build in environment."
                )

            body = {
                "test": original.test_data,
                "environments": [create_new_environment(env) for env in original.environments_data],
            }

            response = await asyncio.to_thread(_testing_farm_api_post, "requests", json=body)
            new_request = _parse_tf_request(response)

        except Exception as e:
            raise ToolError(f"Failed to reproduce Testing Farm request {request_id}: {e}") from e

        return JSONToolOutput(result=new_request.model_dump(mode="json"))


class ReserveTestingFarmMachineToolInput(BaseModel):
    compose: str = Field(description="Compose to reserve, e.g. RHEL-9.8.0-Nightly")
    arch: str = Field(default="x86_64", description="Architecture of the machine")
    duration_minutes: int = Field(default=60, description="Reservation duration in minutes")
    ssh_public_key: str | None = Field(
        default=None,
        description="SSH public key content. If omitted, the gateway's own key is used (recommended).",
    )


class ReserveTestingFarmMachineTool(
    Tool[ReserveTestingFarmMachineToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "reserve_testing_farm_machine"
    description = """
    Reserve a Testing Farm machine for SSH access.
    """
    input_schema = ReserveTestingFarmMachineToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "testing_farm", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: ReserveTestingFarmMachineToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        logger.info(
            "Reserving Testing Farm machine: compose=%s arch=%s duration=%dm",
            tool_input.compose,
            tool_input.arch,
            tool_input.duration_minutes,
        )

        if os.getenv("DRY_RUN", "False").lower() == "true":
            return JSONToolOutput(
                result={
                    "id": "dry-run-reservation",
                    "message": (
                        f"Dry run: would reserve {tool_input.compose} {tool_input.arch} "
                        f"for {tool_input.duration_minutes}m"
                    ),
                }
            )

        try:
            # Always use the gateway's own SSH key so run_remote_command can authenticate
            ssh_public_key = await asyncio.to_thread(_ensure_gateway_ssh_key)
            if tool_input.ssh_public_key and tool_input.ssh_public_key != ssh_public_key:
                logger.info(
                    "Ignoring agent-provided SSH key; using gateway's own key for TF reservation "
                    "(run_remote_command runs in the gateway container)"
                )
            ssh_key_b64 = base64.b64encode(ssh_public_key.encode()).decode()

            body = {
                "test": {
                    "fmf": {
                        "url": "https://gitlab.com/testing-farm/tests",
                        "ref": "main",
                        "name": "/testing-farm/reserve",
                    }
                },
                "environments": [
                    {
                        "arch": tool_input.arch,
                        "os": {"compose": tool_input.compose},
                        "variables": {
                            "TF_RESERVATION_DURATION": str(tool_input.duration_minutes),
                        },
                        "secrets": {
                            "TF_RESERVATION_AUTHORIZED_KEYS_BASE64": ssh_key_b64,
                        },
                        "settings": {
                            "provisioning": {
                                "security_group_rules_ingress": [
                                    {
                                        "type": "ingress",
                                        "protocol": "tcp",
                                        "cidr": "0.0.0.0/0",
                                        "port_min": 22,
                                        "port_max": 22,
                                    }
                                ],
                                "security_group_rules_egress": [],
                            }
                        },
                    }
                ],
                "settings": {
                    "pipeline": {
                        "timeout": max(tool_input.duration_minutes, 720),
                    }
                },
            }

            response = await asyncio.to_thread(_testing_farm_api_post, "requests", json=body)
        except Exception as e:
            raise ToolError(
                f"Failed to reserve Testing Farm machine: {e}"
            ) from e

        return JSONToolOutput(result={"id": response["id"]})


class GetTestingFarmReservationDetailsToolInput(BaseModel):
    request_id: str = Field(description="Testing Farm reservation request ID", pattern=r"^[a-zA-Z0-9_-]+$")


class GetTestingFarmReservationDetailsTool(
    Tool[GetTestingFarmReservationDetailsToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "get_testing_farm_reservation_details"
    description = """
    Get the status and SSH details of a Testing Farm reservation.
    Polls internally for up to 10 minutes until SSH is available or a terminal state is reached.
    Do NOT wrap this tool in a retry loop — it handles waiting internally.
    """
    input_schema = GetTestingFarmReservationDetailsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "testing_farm", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetTestingFarmReservationDetailsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        logger.info("Getting Testing Farm reservation details for %s", tool_input.request_id)

        if os.getenv("DRY_RUN", "False").lower() == "true":
            return JSONToolOutput(
                result={"state": "complete", "ssh_connection": "root@dry-run-host"}
            )

        max_attempts = 20
        poll_interval = 30
        state = "unknown"

        _TRANSIENT_HTTP_CODES = (502, 503, 504)

        for attempt in range(1, max_attempts + 1):
            try:
                response = await asyncio.to_thread(_testing_farm_api_get, f"requests/{tool_input.request_id}")
            except requests.RequestException as e:
                is_transient = False
                if isinstance(e, requests.HTTPError) and e.response is not None:
                    if e.response.status_code in _TRANSIENT_HTTP_CODES:
                        is_transient = True
                elif isinstance(e, (requests.ConnectionError, requests.Timeout)):
                    is_transient = True

                if is_transient:
                    logger.warning(
                        "Transient error %s polling TF %s (attempt %d/%d)",
                        e, tool_input.request_id, attempt, max_attempts,
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(poll_interval)
                    continue
                raise ToolError(
                    f"Failed to get Testing Farm reservation details {tool_input.request_id}: {e}"
                ) from e
            except Exception as e:
                raise ToolError(
                    f"Failed to get Testing Farm reservation details {tool_input.request_id}: {e}"
                ) from e

            state = response.get("state", "unknown")

            if state in ("complete", "canceled", "cancel-requested", "error"):
                return JSONToolOutput(result={"state": state, "ssh_connection": "not-yet-available"})

            if state == "running":
                artifacts_url = (response.get("run") or {}).get("artifacts")
                if artifacts_url:
                    try:
                        log_url = f"{artifacts_url}/pipeline.log"
                        log_resp = await asyncio.to_thread(requests.get, log_url, timeout=30)
                        if log_resp.ok:
                            log_text = log_resp.text
                            guest_match = re.search(
                                r"Guest is ready.*root@([\d\w.\-]+)", log_text
                            )
                            if not guest_match:
                                guest_match = re.search(
                                    r"\[.*?\]\s+primary address:\s+([\d\w.\-]+)", log_text
                                )
                            ready = "execute task #1" in log_text
                            if guest_match and ready:
                                ssh_connection = f"root@{guest_match.group(1)}"
                                logger.info(
                                    "SSH available for %s: %s (attempt %d)",
                                    tool_input.request_id, ssh_connection, attempt,
                                )
                                return JSONToolOutput(
                                    result={"state": state, "ssh_connection": ssh_connection}
                                )
                    except Exception:
                        logger.debug("Could not fetch pipeline.log for %s", tool_input.request_id)

            if attempt < max_attempts:
                logger.info(
                    "SSH not yet available for %s, polling again in %ds (attempt %d/%d)",
                    tool_input.request_id, poll_interval, attempt, max_attempts,
                )
                await asyncio.sleep(poll_interval)

        return JSONToolOutput(result={"state": state, "ssh_connection": "not-yet-available"})


class CancelTestingFarmRequestToolInput(BaseModel):
    request_id: str = Field(description="Testing Farm request ID to cancel", pattern=r"^[a-zA-Z0-9_-]+$")


class CancelTestingFarmRequestTool(
    Tool[CancelTestingFarmRequestToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "cancel_testing_farm_request"
    description = """
    Cancel a Testing Farm request and release the reserved machine.
    """
    input_schema = CancelTestingFarmRequestToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "testing_farm", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: CancelTestingFarmRequestToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        request_id = tool_input.request_id
        logger.info("Cancelling Testing Farm request %s", request_id)

        if os.getenv("DRY_RUN", "False").lower() == "true":
            return JSONToolOutput(
                result={
                    "cancelled": True,
                    "request_id": request_id,
                    "message": f"Dry run: would cancel request {request_id}",
                }
            )

        try:
            await asyncio.to_thread(_testing_farm_api_delete, f"requests/{request_id}")
        except Exception as e:
            raise ToolError(
                f"Failed to cancel Testing Farm request {request_id}: {e}"
            ) from e

        return JSONToolOutput(result={"cancelled": True, "request_id": request_id})


