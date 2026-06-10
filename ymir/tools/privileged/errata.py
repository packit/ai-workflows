import asyncio
import logging
import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from functools import cache
from typing import Any

import requests
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, StringToolOutput, Tool, ToolError, ToolRunOptions
from bs4 import BeautifulSoup, Tag  # type: ignore
from pydantic import BaseModel, Field
from requests_gssapi import HTTPSPNEGOAuth

from ymir.common.models import (
    ErrataComment,
    ErrataStatus,
    Erratum,
    ErratumBuild,
    ErratumBuildMap,
    ErratumPackageFileList,
    ErratumPushDetails,
    ErratumPushStatus,
    FullErratum,
    RHELRelease,
    RHELVersion,
    TransitionRule,
    TransitionRuleOutcome,
    TransitionRuleSet,
)
logger = logging.getLogger(__name__)

ET_URL = "https://errata.engineering.redhat.com"

# regex pattern for extracting timestamps from push logs
_TIMESTAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \+0000")

# Compares correctly - all our dates are tz-aware
_DATETIME_MIN_UTC = datetime.min.replace(tzinfo=UTC)


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
        timeout=30,
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


# -- Private helpers for new tools --


def _skip_writes() -> bool:
    return os.getenv("DRY_RUN", "False").lower() == "true"


def _et_api_post(path: str, data: dict[str, Any]) -> Any | None:
    response = requests.post(
        f"{ET_URL}/api/v1/{path}",
        data=data,
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=_et_verify(),
        timeout=30,
    )
    response.raise_for_status()
    return None


def _et_api_put(path: str, data: dict[str, Any]) -> None:
    response = requests.put(
        f"{ET_URL}/api/v1/{path}",
        data=data,
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=_et_verify(),
        timeout=30,
    )
    response.raise_for_status()


def _et_get_html(path: str) -> str:
    response = requests.get(
        f"{ET_URL}/{path}",
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=_et_verify(),
        timeout=30,
    )
    response.raise_for_status()
    return response.text


def _variant_to_base_variant(variant: str) -> str:
    return variant.split("-")[0]


def _nvr_to_package_name(nvr: str) -> str:
    return nvr.rsplit("-", 2)[0]


def _get_erratum_build_map(erratum_id: int | str) -> ErratumBuildMap:
    data = _et_api_get(f"erratum/{erratum_id}/builds_list")

    if len(data) != 1:
        raise ValueError("Expected JSON object to have a single product version key.")

    detail = next(iter(data.values()))
    builds = detail.get("builds", [])
    build_map = {}

    for build in builds:
        if len(build) != 1:
            raise ValueError("Expected build to have a single NVR key.")

        (nvr, build_detail) = next(iter(build.items()))
        variant_arch = build_detail["variant_arch"]

        package_file_map = {
            _variant_to_base_variant(variant): {
                arch: {
                    _nvr_to_package_name(
                        rpm["filename"] if not isinstance(rpm, str) else rpm
                    )
                    for rpm in rpms
                }
                for arch, rpms in arches.items()
            }
            for variant, arches in variant_arch.items()
        }

        build_map[_nvr_to_package_name(nvr)] = ErratumBuild(
            nvr=nvr, package_file_list=ErratumPackageFileList(root=package_file_map)
        )

    return ErratumBuildMap(root=build_map)


def _get_RHEL_release(param: int | str) -> RHELRelease:
    response = (
        _et_api_get("releases", params={"filter[id]": param})
        if isinstance(param, int)
        else _et_api_get("releases", params={"filter[name]": param})
    )
    release_data = response["data"][0]

    ship_date_string = release_data["attributes"]["ship_date"]
    ship_date = _get_utc_timestamp_from_str(ship_date_string) if ship_date_string is not None else None

    return RHELRelease(
        version=release_data["attributes"]["name"],
        ship_date=ship_date,
    )


def _get_erratum_build_nvr(erratum_id: str | int, package_name: str) -> str | None:
    builds_by_release = _et_api_get(f"erratum/{erratum_id}/builds_list")
    for release_info in builds_by_release.values():
        for builds_map in release_info["builds"]:
            for build_nvr in builds_map:
                if build_nvr.rsplit("-", 2)[0] == package_name:
                    return build_nvr
    return None


def _get_rel_prep_lookup(package_name: str) -> defaultdict[str, list[Erratum]]:
    rel_prep_lookup: defaultdict[str, list[Erratum]] = defaultdict(list)
    package_data = _et_api_get("packages", params={"name": package_name})
    related_errata = package_data["data"]["relationships"]["errata"]
    if not isinstance(related_errata, list):
        raise TypeError(f"expected list of errata, got {type(related_errata)}")
    for erratum_info in related_errata:
        if erratum_info["status"] != ErrataStatus.REL_PREP:
            continue

        id = erratum_info["id"]
        cur_erratum = _get_erratum(id)
        cur_release = _get_RHEL_release(cur_erratum.release_id)

        rel_prep_lookup[cur_release.version].append(cur_erratum)

    return rel_prep_lookup


def _get_previous_erratum(
    current_erratum_id: str | int, package_name: str
) -> tuple[None, None] | tuple[None, str] | tuple[int, str]:
    erratum = _get_erratum(current_erratum_id)

    target_release = _get_RHEL_release(erratum.release_id)
    target_version = RHELVersion.from_str(target_release.version)
    if target_version is None:
        logger.info(f"Unknown RHEL release format: {target_release.version}")
        return (None, None)

    def is_previous_erratum_applicable(erratum_version: str, erratum: Erratum):
        if erratum_version == target_version:
            return True
        if target_release.shipped:
            return False
        if target_release.ship_date is None:
            raise ValueError("target_release.ship_date must be set for unshipped releases")
        return erratum.publish_date is not None and erratum.publish_date <= target_release.ship_date

    rel_prep_lookup = _get_rel_prep_lookup(package_name)
    cur_version = target_version
    while cur_version:
        rel_prep_errata = rel_prep_lookup[str(cur_version)]
        rel_prep = [e for e in rel_prep_errata if is_previous_erratum_applicable(str(cur_version), e)]

        if rel_prep:
            latest_erratum = max(
                rel_prep,
                key=lambda e: e.publish_date if e.publish_date else _DATETIME_MIN_UTC,
            )

            nvr = _get_erratum_build_nvr(latest_erratum.id, package_name)

            if nvr is None:
                raise RuntimeError(
                    f"{latest_erratum.id}, returned by Errata tool as an errata "
                    f"for {package_name}, does not have a build for {package_name}"
                )

            return (latest_erratum.id, nvr)

        release = _get_RHEL_release(str(cur_version))
        if release.shipped:
            released_build = _et_api_get(
                f"product_versions/{release.version}/released_builds/{package_name}"
            )

            erratum_id_from_released_build: int | None = released_build["errata_id"]
            nvr: str | None = released_build["build"]

            if nvr is None:
                return (None, None)
            if erratum_id_from_released_build is None:
                return (None, nvr)
            return (erratum_id_from_released_build, nvr)

        cur_version = cur_version.parent

    return (None, None)


class RuleParseError(Exception):
    pass


def _get_erratum_transition_rules(erratum_id: int | str) -> TransitionRuleSet:
    html = _et_get_html(f"/workflow_rules/for_advisory/{erratum_id}")
    soup = BeautifulSoup(html, "lxml")

    tbody = soup.tbody
    if tbody is None:
        raise RuleParseError("No tbody found")

    rows = tbody.find_all("tr")
    transition_row = rows[0]
    if not isinstance(transition_row, Tag):
        raise RuleParseError("Expected a Tag for transition row")

    spans = transition_row.find_all("span")
    states = [
        span.text
        for span in spans
        if isinstance(span, Tag) and "state_indicator" in span.attrs.get("class", "")
    ]
    if len(states) != 2:
        raise RuleParseError("Couldn't find from and to states")

    def text_to_status(text: str) -> ErrataStatus:
        text = text.strip().upper().replace(" ", "_")
        if text == "SHIPPED":
            return ErrataStatus.SHIPPED_LIVE
        return ErrataStatus(text)

    from_status = text_to_status(states[0])
    to_status = text_to_status(states[1])

    res: list[TransitionRule] = []

    for row in rows[1:]:
        if not isinstance(row, Tag):
            continue

        tds = row.find_all("td")
        if len(tds) != 3:
            raise RuleParseError("Invalid number of columns")

        guard_type, test_type, status = tds
        if not isinstance(guard_type, Tag) or not isinstance(test_type, Tag) or not isinstance(status, Tag):
            raise RuleParseError("Expected Tag elements for columns")

        if guard_type.text != "Block":
            continue
        name = test_type.text
        span = status.span
        if span is None:
            raise RuleParseError("No <span/> found in rule status element")
        className = span.attrs.get("class", "")
        if "step-status-block" in className:
            outcome = TransitionRuleOutcome.BLOCK
        elif "step-status-ok" in className:
            outcome = TransitionRuleOutcome.OK
        else:
            outcome = TransitionRuleOutcome.UNKNOWN

        res.append(TransitionRule(name=name, outcome=outcome, details=status.text.strip()))

    return TransitionRuleSet(
        from_status=from_status,
        to_status=to_status,
        rules=res,
    )


def _get_erratum_stage_push_details(erratum_id: int | str) -> ErratumPushDetails:
    pushes = _et_api_get(f"erratum/{erratum_id}/push")

    highest_push_id = 0
    status = None
    log = None
    for push in pushes:
        if push["target"]["name"] == "cdn_stage" and push["id"] > highest_push_id:
            highest_push_id = push["id"]
            status = push["status"]
            log = push.get("log", "")

    updated_at = None
    if log:
        timestamps = _TIMESTAMP_PATTERN.findall(log)
        if timestamps:
            last_timestamp = timestamps[-1]
            updated_at = datetime.strptime(last_timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

    return ErratumPushDetails(status=ErratumPushStatus(status) if status else None, updated_at=updated_at)


# -- New MCP Tools --


class GetErratumTransitionRulesToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")


class GetErratumTransitionRulesTool(
    Tool[GetErratumTransitionRulesToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "get_erratum_transition_rules"
    description = """
    Scrape the Errata Tool HTML to get state transition guard rules for an erratum.
    Returns the from/to status and list of blocking rules with their outcomes.
    """
    input_schema = GetErratumTransitionRulesToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetErratumTransitionRulesToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        erratum_id = tool_input.erratum_id
        logger.info("Getting transition rules for erratum %s", erratum_id)
        try:
            rule_set = await asyncio.to_thread(_get_erratum_transition_rules, erratum_id)
        except Exception as e:
            raise ToolError(f"Failed to get transition rules for erratum {erratum_id}: {e}") from e
        return JSONToolOutput(result=rule_set.model_dump(mode="json"))


class GetErratumBuildMapToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")


class GetErratumBuildMapTool(
    Tool[GetErratumBuildMapToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "get_erratum_build_map"
    description = """
    Get the build map for an erratum: maps package names to NVR + package file lists.
    """
    input_schema = GetErratumBuildMapToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetErratumBuildMapToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        erratum_id = tool_input.erratum_id
        logger.info("Getting build map for erratum %s", erratum_id)
        try:
            build_map = await asyncio.to_thread(_get_erratum_build_map, erratum_id)
        except Exception as e:
            raise ToolError(f"Failed to get build map for erratum {erratum_id}: {e}") from e
        return JSONToolOutput(result=build_map.model_dump(mode="json"))


class GetPreviousErratumToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")
    package_name: str = Field(description="Package name")


class GetPreviousErratumTool(
    Tool[GetPreviousErratumToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "get_previous_erratum"
    description = """
    Search backwards through RHEL release versions to find the previous erratum
    for a given package, following the RHEL version inheritance chain.
    Returns dict with 'id' (int or null) and 'nvr' (str or null).
    """
    input_schema = GetPreviousErratumToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetPreviousErratumToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        erratum_id = tool_input.erratum_id
        package_name = tool_input.package_name
        logger.info("Getting previous erratum for %s in erratum %s", package_name, erratum_id)
        try:
            prev_id, prev_nvr = await asyncio.to_thread(
                _get_previous_erratum, erratum_id, package_name
            )
        except Exception as e:
            raise ToolError(
                f"Failed to get previous erratum for {package_name} in {erratum_id}: {e}"
            ) from e
        return JSONToolOutput(result={"id": prev_id, "nvr": prev_nvr})


class GetErratumStagePushDetailsToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")


class GetErratumStagePushDetailsTool(
    Tool[GetErratumStagePushDetailsToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "get_erratum_stage_push_details"
    description = """
    Get the latest stage push status and timestamp for an erratum.
    """
    input_schema = GetErratumStagePushDetailsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetErratumStagePushDetailsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        erratum_id = tool_input.erratum_id
        logger.info("Getting stage push details for erratum %s", erratum_id)
        try:
            details = await asyncio.to_thread(_get_erratum_stage_push_details, erratum_id)
        except Exception as e:
            raise ToolError(
                f"Failed to get stage push details for erratum {erratum_id}: {e}"
            ) from e
        return JSONToolOutput(result=details.model_dump(mode="json"))


class ErratumPushToStageToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")


class ErratumPushToStageTool(
    Tool[ErratumPushToStageToolInput, ToolRunOptions, StringToolOutput]
):
    name = "erratum_push_to_stage"
    description = """
    Push an erratum to the CDN stage environment. Respects DRY_RUN.
    """
    input_schema = ErratumPushToStageToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: ErratumPushToStageToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        erratum_id = tool_input.erratum_id
        if _skip_writes():
            return StringToolOutput(
                result=f"Dry run, not pushing erratum {erratum_id} to stage "
                f"(this is expected, not an error)"
            )
        logger.info("Pushing erratum %s to stage", erratum_id)
        try:
            await asyncio.to_thread(
                _et_api_post, f"erratum/{erratum_id}/push", {"defaults": "stage"}
            )
        except Exception as e:
            raise ToolError(f"Failed to push erratum {erratum_id} to stage: {e}") from e
        return StringToolOutput(result=f"Successfully pushed erratum {erratum_id} to stage")


class ErratumChangeStateToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")
    new_state: str = Field(description="New state (e.g. 'QE', 'REL_PREP')")


class ErratumChangeStateTool(
    Tool[ErratumChangeStateToolInput, ToolRunOptions, StringToolOutput]
):
    name = "erratum_change_state"
    description = """
    Change the state of an erratum. Respects DRY_RUN.
    """
    input_schema = ErratumChangeStateToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: ErratumChangeStateToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        erratum_id = tool_input.erratum_id
        new_state = tool_input.new_state
        if _skip_writes():
            return StringToolOutput(
                result=f"Dry run, not changing state of erratum {erratum_id} to {new_state} "
                f"(this is expected, not an error)"
            )
        logger.info("Changing state of erratum %s to %s", erratum_id, new_state)
        try:
            await asyncio.to_thread(
                _et_api_post,
                f"erratum/{erratum_id}/change_state",
                {"new_state": new_state},
            )
        except Exception as e:
            raise ToolError(
                f"Failed to change state of erratum {erratum_id} to {new_state}: {e}"
            ) from e
        return StringToolOutput(
            result=f"Successfully changed state of erratum {erratum_id} to {new_state}"
        )


class ErratumChangeOwnershipToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")
    new_owner_email: str = Field(description="New owner email address")


class ErratumChangeOwnershipTool(
    Tool[ErratumChangeOwnershipToolInput, ToolRunOptions, StringToolOutput]
):
    name = "erratum_change_ownership"
    description = """
    Change the ownership (assigned_to and package_owner) of an erratum. Respects DRY_RUN.
    """
    input_schema = ErratumChangeOwnershipToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: ErratumChangeOwnershipToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        erratum_id = tool_input.erratum_id
        new_owner_email = tool_input.new_owner_email
        if _skip_writes():
            return StringToolOutput(
                result=f"Dry run, not changing ownership of erratum {erratum_id} "
                f"(this is expected, not an error)"
            )
        logger.info("Changing ownership of erratum %s to %s", erratum_id, new_owner_email)
        try:
            await asyncio.to_thread(
                _et_api_put,
                f"erratum/{erratum_id}",
                {
                    "advisory[assigned_to_email]": new_owner_email,
                    "advisory[package_owner_email]": new_owner_email,
                },
            )
        except Exception as e:
            raise ToolError(
                f"Failed to change ownership of erratum {erratum_id}: {e}"
            ) from e
        return StringToolOutput(
            result=f"Successfully changed ownership of erratum {erratum_id} to {new_owner_email}"
        )


class ErratumAddCommentToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")
    comment: str = Field(description="Comment text")


class ErratumAddCommentTool(
    Tool[ErratumAddCommentToolInput, ToolRunOptions, StringToolOutput]
):
    name = "erratum_add_comment"
    description = """
    Add a comment to an erratum. Respects DRY_RUN.
    """
    input_schema = ErratumAddCommentToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: ErratumAddCommentToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        erratum_id = tool_input.erratum_id
        comment = tool_input.comment
        if _skip_writes():
            return StringToolOutput(
                result=f"Dry run, not adding comment to erratum {erratum_id} "
                f"(this is expected, not an error)"
            )
        logger.info("Adding comment to erratum %s", erratum_id)
        try:
            await asyncio.to_thread(
                _et_api_post,
                f"erratum/{erratum_id}/add_comment",
                {"comment": comment},
            )
        except Exception as e:
            raise ToolError(f"Failed to add comment to erratum {erratum_id}: {e}") from e
        return StringToolOutput(
            result=f"Successfully added comment to erratum {erratum_id}"
        )


class ErratumRefreshSecurityAlertsToolInput(BaseModel):
    erratum_id: str = Field(description="Erratum ID")


class ErratumRefreshSecurityAlertsTool(
    Tool[ErratumRefreshSecurityAlertsToolInput, ToolRunOptions, StringToolOutput]
):
    name = "erratum_refresh_security_alerts"
    description = """
    Refresh security alerts for an erratum. Respects DRY_RUN.
    """
    input_schema = ErratumRefreshSecurityAlertsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "errata", self.name], creator=self)

    async def _run(
        self,
        tool_input: ErratumRefreshSecurityAlertsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        erratum_id = tool_input.erratum_id
        if _skip_writes():
            return StringToolOutput(
                result=f"Dry run, not refreshing security alerts for erratum {erratum_id} "
                f"(this is expected, not an error)"
            )
        logger.info("Refreshing security alerts for erratum %s", erratum_id)
        try:
            await asyncio.to_thread(
                _et_api_post,
                f"erratum/{erratum_id}/security_alerts/refresh",
                {},
            )
        except Exception as e:
            raise ToolError(
                f"Failed to refresh security alerts for erratum {erratum_id}: {e}"
            ) from e
        return StringToolOutput(
            result=f"Successfully refreshed security alerts for erratum {erratum_id}"
        )
