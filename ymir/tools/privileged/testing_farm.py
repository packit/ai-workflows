import logging
import os
from datetime import datetime
from functools import cache
from json import dumps as json_dumps
from typing import Any

import requests
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.common.models import (
    TestingFarmRequest,
    TestingFarmRequestResult,
)

logger = logging.getLogger(__name__)

TESTING_FARM_URL = "https://api.testing-farm.io/v0.1"


@cache
def _testing_farm_headers() -> dict[str, str]:
    token = os.environ["TESTING_FARM_API_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _testing_farm_api_get(path: str, *, params: dict | None = None) -> Any:
    url = f"{TESTING_FARM_URL}/{path}"
    response = requests.get(url, headers=_testing_farm_headers(), params=params)
    if not response.ok:
        logger.error(
            "GET %s%s failed.\nerror:\n%s", url, f" (params={params})" if params else "", response.text
        )
    response.raise_for_status()
    return response.json()


def _testing_farm_api_post(path: str, json: dict[str, Any]) -> Any:
    url = f"{TESTING_FARM_URL}/{path}"
    response = requests.post(url, headers=_testing_farm_headers(), json=json)
    if not response.ok:
        logger.error(
            "POST to %s failed\nbody:\n%s\nerror:\n%s", url, json_dumps(json, indent=2), response.text
        )
    response.raise_for_status()
    return response.json()


def _parse_tf_request(response: dict[str, Any]) -> TestingFarmRequest:
    result_data = response.get("result")
    result = result_data["overall"] if result_data else TestingFarmRequestResult.UNKNOWN
    error_reason = result_data.get("summary") if result == TestingFarmRequestResult.ERROR else None

    return TestingFarmRequest(
        id=response["id"],
        url=f"{TESTING_FARM_URL}/requests/{response['id']}",
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
            response = _testing_farm_api_get(f"requests/{tool_input.request_id}")
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
            original_response = _testing_farm_api_get(f"requests/{request_id}")
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

            response = _testing_farm_api_post("requests", json=body)
            new_request = _parse_tf_request(response)

        except Exception as e:
            raise ToolError(f"Failed to reproduce Testing Farm request {request_id}: {e}") from e

        return JSONToolOutput(result=new_request.model_dump(mode="json"))
