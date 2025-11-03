from datetime import datetime, timezone
from functools import cache
from json import dumps as json_dumps
import logging
import os
from typing import Any


from .http_utils import requests_session
from .supervisor_types import (
    TestingFarmRequest,
    TestingFarmRequestResult,
    TestingFarmRequestState,
)


TESTING_FARM_URL = "https://api.testing-farm.io/v0.1"


logger = logging.getLogger(__name__)


@cache
def testing_farm_headers() -> dict[str, str]:
    token = os.environ["TESTING_FARM_API_TOKEN"]

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def testing_farm_api_get(path: str, *, params: dict | None = None) -> Any:
    url = f"{TESTING_FARM_URL}/{path}"
    response = requests_session().get(
        url, headers=testing_farm_headers(), params=params
    )
    if not response.ok:
        logger.error(
            "GET %s%s failed.\nerror:\n%s",
            url,
            f" (params={params})" if params else "",
            response.text,
        )
    response.raise_for_status()
    return response.json()


def testing_farm_api_post(
    path: str,
    json: dict[str, Any],
) -> Any:
    url = f"{TESTING_FARM_URL}/{path}"
    response = requests_session().post(url, headers=testing_farm_headers(), json=json)
    if not response.ok:
        logger.error(
            "POST to %s failed\nbody:\n%s\nerror:\n%s",
            url,
            json_dumps(json, indent=2),
            response.text,
        )
    response.raise_for_status()
    return response.json()


fake_testing_id_counter = 0


def testing_farm_reproduce_request_with_build(
    request: TestingFarmRequest,
    build_nvr: str,
    dry_run: bool = False,
) -> TestingFarmRequest:
    """
    Create a Testing Farm request to reproduce an existing request with a different build.

    Args:
        request: The original Testing Farm request.
        build_nvr: The NVR of the build to use for reproduction.

    Returns:
        The new Testing Farm request.

    Raises:
        HTTPError: If the API request fails.
    """
    original_environments = request.environments_data

    # We manually construct the environment to replace the build
    # and skip newa_ variables. There are some other keys in the
    # environment dict that we don't copy over - in particular
    # "hardware" and "kickstart" - these shouldn't be relevant.
    environments = [
        {
            "arch": env["arch"],
            "os": env["os"],
            "variables": env["variables"] | {"BUILDS": build_nvr},
            "tmt": {
                "context": {
                    k: v
                    for k, v in env["tmt"]["context"].items()
                    if not k.startswith("newa_")
                }
            },
        }
        for env in original_environments
    ]

    body = {
        "test": request.test_data,
        "environments": environments,
    }
    if dry_run:
        logger.info(
            "Dry run: would start Testing Farm request reproducing %s with build %s",
            request.id,
            build_nvr,
        )
        logger.debug("Dry run: would post %s to %s", body, "requests")
        global fake_testing_id_counter
        fake_testing_id_counter += 1
        test_id = f"fake-testing-id-{fake_testing_id_counter}"
        return TestingFarmRequest(
            id=test_id,
            url=f"{TESTING_FARM_URL}/requests/{test_id}",
            state=TestingFarmRequestState.NEW,
            created=datetime.now(tz=timezone.utc),
            updated=datetime.now(tz=timezone.utc),
            test_data=body["test"],
            environments_data=body["environments"],
        )

    response = testing_farm_api_post("requests", json=body)

    return TestingFarmRequest(
        id=response["id"],
        url=f"{TESTING_FARM_URL}/requests/{response['id']}",
        state=response["state"],
        created=datetime.fromisoformat(response["created"]),
        updated=datetime.fromisoformat(response["updated"]),
        test_data=response["test"],
        environments_data=response["environments"],
    )


def testing_farm_get_request(request_id: str) -> TestingFarmRequest:
    """
    Retrieve details about a Testing Farm request by its ID.

    Args:
        request_id: The ID of the Testing Farm request.

    Returns:
        The Testing Farm request.
    """
    response = testing_farm_api_get(f"requests/{request_id}")

    result_data = response.get("result")
    result = result_data["overall"] if result_data else TestingFarmRequestResult.UNKNOWN
    # We have a specific error_reason field rather than a general summary field
    # to avoid models relying on a summary rather than doing their own analysis.
    error_reason = (
        result_data.get("summary") if result == TestingFarmRequestResult.ERROR else None
    )

    return TestingFarmRequest(
        id=response["id"],
        url=f"{TESTING_FARM_URL}/requests/{response['id']}",
        state=response["state"],
        result=result,
        error_reason=error_reason,
        result_xunit_url=result_data.get("xunit_url") if result_data else None,
        created=datetime.fromisoformat(response["created"]),
        updated=datetime.fromisoformat(response["updated"]),
        test_data=response["test"],
        environments_data=response["environments_requested"],
    )
