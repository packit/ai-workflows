import asyncio
import logging
import os
from datetime import UTC, datetime
from functools import cache
from typing import Any

import requests
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolError, ToolRunOptions
from pydantic import BaseModel, Field
from requests_gssapi import HTTPSPNEGOAuth

from ymir.common.models import ErrataComment, ErrataStatus, Erratum, FullErratum

logger = logging.getLogger(__name__)

ET_URL = "https://errata.engineering.redhat.com"


@cache
def _et_verify() -> bool | str:
    verify = os.getenv("REDHAT_IT_CA_BUNDLE")
    if verify:
        return verify
    return True


def _et_api_get(path: str, *, params: dict | None = None) -> Any:
    response = requests.get(
        f"{ET_URL}/api/v1/{path}",
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=_et_verify(),
        params=params,
    )
    response.raise_for_status()
    return response.json()


def _get_utc_timestamp_from_str(timestamp_string: str) -> datetime:
    return datetime.strptime(timestamp_string, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@cache
def _get_errata_user_email(id: int | str) -> str:
    response = _et_api_get(f"user/{id}")
    return response["login_name"]


def _get_erratum(erratum_id: str | int, *, full: bool = False) -> Erratum | FullErratum:
    data = _et_api_get(f"erratum/{erratum_id}")
    erratum_data = data["errata"]

    if "rhba" in erratum_data:
        details = erratum_data["rhba"]
    elif "rhsa" in erratum_data:
        details = erratum_data["rhsa"]
    elif "rhea" in erratum_data:
        details = erratum_data["rhea"]
    else:
        raise ValueError("Unknown erratum type")

    jira_issues = [i["jira_issue"]["key"] for i in data["jira_issues"]["jira_issues"]]

    last_status_transition_timestamp = _get_utc_timestamp_from_str(details["status_updated_at"])
    publish_date = (
        _get_utc_timestamp_from_str(details["publish_date"]) if details["publish_date"] is not None else None
    )
    assigned_to_email = _get_errata_user_email(details["assigned_to_id"])
    package_owner_email = _get_errata_user_email(details["package_owner_id"])

    base = Erratum(
        id=details["id"],
        full_advisory=details["fulladvisory"],
        url=f"{ET_URL}/advisory/{erratum_id}",
        synopsis=details["synopsis"],
        status=ErrataStatus(details["status"]),
        jira_issues=jira_issues,
        release_id=details["group_id"],
        publish_date=publish_date,
        last_status_transition_timestamp=last_status_transition_timestamp,
        assigned_to_email=assigned_to_email,
        package_owner_email=package_owner_email,
    )

    if full:
        comments = _get_erratum_comments(erratum_id)
        return FullErratum(**base.__dict__, comments=comments)
    return base


def _get_erratum_comments(erratum_id: str | int) -> list[ErrataComment] | None:
    data = _et_api_get(f"comments?filter[errata_id]={erratum_id}")
    return [
        ErrataComment(
            authorName=comment_data["attributes"]["who"]["realname"],
            authorEmail=comment_data["attributes"]["who"]["login_name"],
            created=datetime.fromisoformat(comment_data["attributes"]["created_at"].replace("Z", "+00:00")),
            body=comment_data["attributes"]["text"],
        )
        for comment_data in data["data"]
    ]


# -- MCP Tools --


class GetErratumToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID or advisory link (e.g. '12345' or full URL)")
    full: bool = Field(default=False, description="If true, include comments in the response")


class GetErratumTool(Tool[GetErratumToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]):
    name = "get_erratum"
    description = """
    Get erratum details (basic or full with comments) by ID or link.
    """
    input_schema = GetErratumToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "errata", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetErratumToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        erratum_id = tool_input.erratum_id
        # Handle URL input — extract the ID from the end
        if "/" in erratum_id:
            erratum_id = erratum_id.rstrip("/").split("/")[-1]

        logger.info("Getting erratum %s (full=%s)", erratum_id, tool_input.full)
        try:
            erratum = await asyncio.to_thread(_get_erratum, erratum_id, full=tool_input.full)
        except Exception as e:
            raise ToolError(f"Failed to get erratum {erratum_id}: {e}") from e

        return JSONToolOutput(result=erratum.model_dump(mode="json"))


class GetErratumBuildNvrToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")
    package_name: str = Field(description="Package name to look up the build NVR for")


class GetErratumBuildNvrTool(Tool[GetErratumBuildNvrToolInput, ToolRunOptions, JSONToolOutput[str | None]]):
    name = "get_erratum_build_nvr"
    description = """
    Get the build NVR for a package in an erratum.
    """
    input_schema = GetErratumBuildNvrToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "errata", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetErratumBuildNvrToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[str | None]:
        erratum_id = tool_input.erratum_id
        package_name = tool_input.package_name
        logger.info("Getting build NVR for %s in erratum %s", package_name, erratum_id)

        try:
            builds_by_release = await asyncio.to_thread(_et_api_get, f"erratum/{erratum_id}/builds_list")
            for release_info in builds_by_release.values():
                for builds_map in release_info["builds"]:
                    for build_nvr in builds_map:
                        if build_nvr.rsplit("-", 2)[0] == package_name:
                            return JSONToolOutput(result=build_nvr)
        except Exception as e:
            raise ToolError(f"Failed to get build NVR for {package_name} in erratum {erratum_id}: {e}") from e

        return JSONToolOutput(result=None)
