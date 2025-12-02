from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
from enum import StrEnum
from functools import cache
import logging
import os
import re
from typing import Any, DefaultDict, overload
from typing_extensions import Literal

from bs4 import BeautifulSoup, Tag  # type: ignore
from pydantic import BaseModel, RootModel
from requests_gssapi import HTTPSPNEGOAuth

from .constants import DATETIME_MIN_UTC
from .http_utils import requests_session
from .supervisor_types import Erratum, FullErratum, ErrataStatus, Comment

logger = logging.getLogger(__name__)


ET_URL = "https://errata.engineering.redhat.com"

# regex pattern for extracting timestamps from push logs
_TIMESTAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \+0000")


@cache
def ET_verify() -> bool | str:
    verify = os.getenv("REDHAT_IT_CA_BUNDLE")
    if verify:
        return verify
    else:
        return True


def ET_api_get(path: str, *, params: dict | None = None):
    response = requests_session().get(
        f"{ET_URL}/api/v1/{path}",
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
        params=params,
    )
    response.raise_for_status()
    return response.json()


@overload
def ET_api_post(
    path: str, data: dict[str, Any], *, decode_response: Literal[False] = False
) -> None: ...


@overload
def ET_api_post(
    path: str, data: dict[str, Any], *, decode_response: Literal[True]
) -> Any: ...


def ET_api_post(
    path: str, data: dict[str, Any], *, decode_response: bool = False
) -> Any | None:
    response = requests_session().post(
        f"{ET_URL}/api/v1/{path}",
        data=data,
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
    )
    response.raise_for_status()

    if decode_response:
        return response.json()


@overload
def ET_api_put(
    path: str, data: dict[str, Any], *, decode_response: Literal[False] = False
) -> None: ...


@overload
def ET_api_put(
    path: str, data: dict[str, Any], *, decode_response: Literal[True]
) -> Any: ...


def ET_api_put(
    path: str, data: dict[str, Any], *, decode_response: bool = False
) -> Any | None:
    response = requests_session().put(
        f"{ET_URL}/api/v1/{path}",
        data=data,
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
    )

    response.raise_for_status()

    if decode_response:
        return response.json()


def ET_get_html(path: str):
    response = requests_session().get(
        f"{ET_URL}/{path}",
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
    )
    response.raise_for_status()
    return response.text


def get_utc_timestamp_from_str(timestamp_string: str):
    return datetime.strptime(timestamp_string, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def get_erratum_advisory_url(erratum_id: str | int) -> str:
    return f"{ET_URL}/advisory/{erratum_id}"


@overload
def get_erratum(erratum_id: str | int, full: Literal[False] = False) -> Erratum: ...


@overload
def get_erratum(erratum_id: str | int, full: Literal[True]) -> FullErratum: ...


def get_erratum(erratum_id: str | int, full: bool = False) -> Erratum | FullErratum:
    logger.debug("Getting detailed information for erratum %s", erratum_id)
    data = ET_api_get(f"erratum/{erratum_id}")
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

    last_status_transition_timestamp = get_utc_timestamp_from_str(
        details["status_updated_at"]
    )
    publish_date = (
        get_utc_timestamp_from_str(details["publish_date"])
        if details["publish_date"] is not None
        else None
    )
    assigned_to_email = get_errata_user_email(details["assigned_to_id"])
    package_owner_email = get_errata_user_email(details["package_owner_id"])

    base_erratum = Erratum(
        id=details["id"],
        full_advisory=details["fulladvisory"],
        url=get_erratum_advisory_url(erratum_id),
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
        # fetching comments for the erratum
        comments = get_erratum_comments(erratum_id)
        return FullErratum(
            **base_erratum.__dict__,
            comments=comments,
        )
    else:
        return base_erratum


def get_erratum_comments(erratum_id: str | int) -> list[Comment] | None:
    """Get all comments for an erratum with the given erratum_id"""
    logger.debug("Getting comments for erratum %s", erratum_id)
    data = ET_api_get(f"comments?filter[errata_id]={erratum_id}")

    return [
        Comment(
            authorName=comment_data["attributes"]["who"]["realname"],
            authorEmail=comment_data["attributes"]["who"]["login_name"],
            created=datetime.fromisoformat(
                comment_data["attributes"]["created_at"].replace("Z", "+00:00")
            ),
            body=comment_data["attributes"]["text"],
        )
        for comment_data in data["data"]
    ]


def erratum_has_magic_string_in_comments(
    erratum_id: int | str, magic_string: str
) -> bool:
    comments = get_erratum_comments(erratum_id)
    if comments is None:
        return False

    return any(magic_string in comment.body for comment in comments)


@overload
def get_erratum_for_link(link: str, full: Literal[False] = False) -> Erratum: ...


@overload
def get_erratum_for_link(link: str, full: Literal[True]) -> FullErratum: ...


def get_erratum_for_link(link: str, full: bool = False) -> Erratum | FullErratum:
    erratum_id = link.split("/")[-1]
    return get_erratum(erratum_id, full=full)


@cache
def get_errata_user_email(id: int | str) -> str:
    response = ET_api_get(f"user/{id}")
    # Using login_name rather than email_address here is intentional - this matches
    # the handling in get_erratum_comments() where only login_name is available,
    # and also when we set assigned_to_email when updating an erratum, it expects
    # the login_name, not the email. (usually they are the same).
    return response["login_name"]


class ErratumBuild(BaseModel):
    nvr: str
    package_file_list: ErratumPackageFileList


class ErratumBuildMap(RootModel):
    # Map package to a build with nvr and package file list
    # {
    #   "libtiff": ErratumBuild({
    #       nvr: libtiff-4.0.9-35.el8_10
    #       package_file_list: {
    #           "AppStream": {
    #               "SRPMS": {"libtiff"}
    #               "aarch64": {"libtiff", "libtiff-devel", ...
    #           }
    #       }
    #   }),
    #   "libjpeg": ....
    # }
    root: dict[str, ErratumBuild]


class ErratumPackageFileList(RootModel):
    # Map variant and architecture to a set of subpackage names
    # we ship for that architecture
    # {
    #     "AppStream": {
    #           "SRPMS": {"libtiff"}
    #           "aarch64": {"libtiff", "libtiff-devel", ...}
    #     }
    # }
    root: dict[str, dict[str, set[str]]]


def variant_to_base_variant(variant: str) -> str:
    return variant.split("-")[0]


def nvr_to_package_name(nvr: str) -> str:
    return nvr.rsplit("-", 2)[0]


def get_erratum_build_map(erratum_id: int | str) -> ErratumBuildMap:
    """Create a build map for the given erratum

    The build map can be used to compare if the subpackages
    match with the other build map for a given package name.

    Throws an exception if more than one RHEL version build
    attached.
    """
    data = ET_api_get(f"erratum/{erratum_id}/builds_list")

    if len(data) != 1:
        raise ValueError("Expected JSON object to have a single product version key.")

    detail = next(iter(data.values()))
    builds = detail.get("builds", [])
    build_map = dict()

    for build in builds:
        if len(build) != 1:
            raise ValueError("Expected build to have a single NVR key.")

        (nvr, build_detail) = next(iter(build.items()))
        variant_arch = build_detail["variant_arch"]

        package_file_map = {
            variant_to_base_variant(variant): {
                arch: set(
                    [
                        nvr_to_package_name(
                            # builds_list API's response has two variant formats
                            rpm["filename"] if not isinstance(rpm, str) else rpm
                        )
                        for rpm in rpms
                    ]
                )
                for arch, rpms in arches.items()
            }
            for variant, arches in variant_arch.items()
        }

        build_map[nvr_to_package_name(nvr)] = ErratumBuild(
            nvr=nvr, package_file_list=ErratumPackageFileList(root=package_file_map)
        )

    return ErratumBuildMap(root=build_map)


class RHELVersion(BaseModel):
    major: int
    minor: int
    micro: int | None
    stream: str

    def __str__(self):
        if self.micro is not None:
            return f"RHEL-{self.major}.{self.minor}.{self.micro}.{self.stream}"

        return f"RHEL-{self.major}.{self.minor}.{self.stream}"

    @property
    def parent(self) -> RHELVersion | None:
        """The release that the release inherits builds from."""

        if self.stream != "GA":
            return RHELVersion(
                major=self.major,
                minor=self.minor,
                micro=self.micro,
                stream="GA",
            )

        if self.minor > 0:
            one_minor_version_up = self.minor - 1
            match self.major:
                case 10:
                    return RHELVersion(
                        major=self.major,
                        minor=one_minor_version_up,
                        micro=self.micro,
                        stream="Z",
                    )
                case 9 | 8:
                    if one_minor_version_up % 2 == 1:
                        return RHELVersion(
                            major=self.major,
                            minor=one_minor_version_up,
                            micro=self.micro,
                            stream="Z.MAIN",
                        )
                    else:
                        return RHELVersion(
                            major=self.major,
                            minor=one_minor_version_up,
                            micro=self.micro,
                            stream="Z.MAIN+EUS",
                        )

        return None

    @staticmethod
    def from_str(version_string: str) -> RHELVersion | None:
        version_string = version_string.strip()
        version_string = version_string.upper()
        pattern = r"RHEL-(\d+)\.(\d+)(?:\.(\d+))?\.([^\d].*)$"
        match = re.match(pattern, version_string)
        if match is not None:
            version = RHELVersion(
                major=int(match.group(1)),
                minor=int(match.group(2)),
                micro=int(match.group(3)) if match.group(3) else None,
                stream=match.group(4),
            )

            assert version_string == str(version)

            return version


class RHELRelease(BaseModel):
    version: str
    # None means already shipped
    ship_date: datetime | None

    @property
    def shipped(self):
        return self.ship_date is None or self.ship_date < datetime.now(tz=timezone.utc)


def get_RHEL_release(param: int | str):
    response = (
        ET_api_get("releases", params={"filter[id]": param})
        if isinstance(param, int)
        else ET_api_get("releases", params={"filter[name]": param})
    )
    release_data = response["data"][0]

    ship_date_string = release_data["attributes"]["ship_date"]
    ship_date = (
        get_utc_timestamp_from_str(ship_date_string)
        if ship_date_string is not None
        else None
    )

    return RHELRelease(
        version=release_data["attributes"]["name"],
        ship_date=ship_date,
    )


def _get_rel_prep_lookup(package_name: str) -> DefaultDict[str, list[Erratum]]:
    """Builds a lookup of REL_PREP errata for a package, keyed by RHEL release version.

    This function queries an API for all errata associated with a given package,
    filters for those in the "REL_PREP" (Release Preparation) state, and organizes
    them into a dictionary where each key is a RHEL version string.

    Args:
        package_name: The name of the package to look up.

    Returns:
        A defaultdict where keys are RHEL release version strings and values are
        lists of associated Erratum objects in the REL_PREP state.
    """
    rel_prep_lookup: DefaultDict[str, list[Erratum]] = defaultdict(list)
    related_errata = ET_api_get(f"packages/{package_name}")["data"]["relationships"][
        "errata"
    ]
    assert isinstance(related_errata, list)
    for erratum_info in related_errata:
        if erratum_info["status"] != ErrataStatus.REL_PREP:
            continue

        id = erratum_info["id"]
        cur_erratum = get_erratum(id)
        cur_release = get_RHEL_release(cur_erratum.release_id)

        rel_prep_lookup[cur_release.version].append(cur_erratum)

    return rel_prep_lookup


def get_previous_erratum(
    current_erratum_id: str | int, package_name: str
) -> tuple[int | str | None, str | None]:
    """Finds the previous erratum for a given package, starting from a specific erratum.

    RHEL releases inherit packages from previous releases, but only until they are shipped.
    This function searches backwards through RHEL release versions starting from the one
    associated with current_erratum_id looking for applicable REL_PREP errata, or for
    a shipped release. If we find a shipped release, we're done - no errata will be inherited
    from previous releases, so if we haven't found a REL_PREP errata first, we use the
    errata associated with the official released build for the package in that release
    and stop.

    Args:
        current_erratum_id: The ID of the erratum to start the search from.
        package_name: The name of the package for which to find the previous erratum.

    Returns:
        A tuple of (previous Erratum id, build nvr). Both previous Erratum id and build nvr
        could be None, please check their values before using.
    """
    erratum = get_erratum(current_erratum_id)

    target_release = get_RHEL_release(erratum.release_id)
    target_version = RHELVersion.from_str(target_release.version)
    if target_version is None:
        logger.info(f"Unknown RHEL release format: {target_release.version}")
        return (None, None)

    def is_previous_erratum_applicable(erratum_version: str, erratum: Erratum):
        if erratum_version == target_version:
            return True
        elif target_release.shipped:
            return False

        assert target_release.ship_date is not None
        return (
            erratum.publish_date is not None
            and erratum.publish_date <= target_release.ship_date
        )

    rel_prep_lookup = _get_rel_prep_lookup(package_name)
    cur_version = target_version
    while cur_version:
        rel_prep_errata = rel_prep_lookup[str(cur_version)]
        rel_prep = [
            e
            for e in rel_prep_errata
            if is_previous_erratum_applicable(str(cur_version), e)
        ]

        if rel_prep:
            latest_erratum = max(
                rel_prep,
                key=lambda e: e.publish_date if e.publish_date else DATETIME_MIN_UTC,
            )

            return (
                latest_erratum.id,
                get_erratum_build_nvr(latest_erratum.id, package_name),
            )

        release = get_RHEL_release(str(cur_version))
        if release.shipped:
            released_build = ET_api_get(
                f"product_versions/{release.version}/released_builds/{package_name}"
            )

            erratum_id_from_released_build: int | str | None = released_build[
                "errata_id"
            ]
            nvr = (
                get_erratum_build_nvr(erratum_id_from_released_build, package_name)
                if erratum_id_from_released_build is not None
                else None
            )

            return (erratum_id_from_released_build, nvr)

        cur_version = cur_version.parent

    return (None, None)


def get_erratum_build_nvr(erratum_id: str | int, package_name: str) -> str | None:
    """
    Gets the build NVR for a package in an erratum.

    Args:
        erratum_id: The ID of the erratum.
        package_name: The name of the package for which to find the build NVR.

    Returns:
        The build NVR for the package in the erratum, or None if not found. If
        the package is included in multiple releases within the erratum, returns
        the first one found. We do not expect builds for multiple product
        versions for the errata we are dealing with - that functionality is for
        products layered on top of RHEL that release to multiple RHEL versions.
    """
    builds_by_release = ET_api_get(f"erratum/{erratum_id}/builds_list")
    for release_info in builds_by_release.values():
        for builds_map in release_info["builds"]:
            for build_nvr in builds_map:
                if build_nvr.rsplit("-", 2)[0] == package_name:
                    return build_nvr

    return None


class RuleParseError(Exception):
    pass


class TransitionRuleOutcome(StrEnum):
    BLOCK = "BLOCK"
    OK = "OK"
    UNKNOWN = "UNKNOWN"


class TransitionRule(BaseModel):
    name: str
    outcome: TransitionRuleOutcome
    details: str


class TransitionRuleSet(BaseModel):
    from_status: ErrataStatus
    to_status: ErrataStatus
    rules: list[TransitionRule]

    @property
    def all_ok(self) -> bool:
        return all(rule.outcome == TransitionRuleOutcome.OK for rule in self.rules)


def get_erratum_transition_rules(erratum_id) -> TransitionRuleSet:
    """
    Gets the status of the "state transition guards" that determine whether an
    erratum can be moved to the next state. (We use the terminology "rule" here
    rather than "guard" for simplicity, since the guard terminology is internal
    to the Errata Tool codebase)

    There is no API for this in the Errata Tool API, so we have to scrape the HTML.
    """

    # If show_all=1 is added to the URL, the table will include rules
    # for all defined state transitions, without it just gives the
    # rules for the current state to the "next" one.
    html = ET_get_html(
        f"/workflow_rules/for_advisory/{erratum_id}",
    )
    soup = BeautifulSoup(html, "lxml")

    tbody = soup.tbody
    if tbody is None:
        raise RuleParseError("No tbody found")

    rows = tbody.find_all("tr")
    transition_row = rows[0]
    # These assertions are because BeautifulSoup's typing doesn't represent
    # the fact that if you find_all() a tag name then you'll only get tags
    assert isinstance(transition_row, Tag)

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
        else:
            return ErrataStatus(text)

    from_status = text_to_status(states[0])
    to_status = text_to_status(states[1])

    res: list[TransitionRule] = []

    for row in rows[1:]:
        assert isinstance(row, Tag)

        tds = row.find_all("td")
        if len(tds) != 3:
            raise RuleParseError("Invalid number of columns")

        guard_type, test_type, status = tds
        assert isinstance(guard_type, Tag)
        assert isinstance(test_type, Tag)
        assert isinstance(status, Tag)

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

        res.append(
            TransitionRule(name=name, outcome=outcome, details=status.text.strip())
        )

    return TransitionRuleSet(
        from_status=from_status,
        to_status=to_status,
        rules=res,
    )


class ErratumPushStatus(StrEnum):
    QUEUED = "QUEUED"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_ON_PUB = "WAITING_ON_PUB"
    POST_PUSH_PROCESSING = "POST_PUSH_PROCESSING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class ErratumPushDetails(BaseModel):
    status: ErratumPushStatus | None
    updated_at: datetime | None


def erratum_get_latest_stage_push_details(erratum_id) -> ErratumPushDetails:
    pushes = ET_api_get(
        f"erratum/{erratum_id}/push",
    )

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
            # last timestamp from logs
            last_timestamp = timestamps[-1]
            updated_at = datetime.strptime(last_timestamp, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )

    return ErratumPushDetails(
        status=ErratumPushStatus(status) if status else None, updated_at=updated_at
    )


def erratum_push_to_stage(erratum_id, *, dry_run: bool = False):
    if dry_run:
        logger.info("Dry run: Would stage push erratum %s to stage", erratum_id)
        return

    ET_api_post(
        f"erratum/{erratum_id}/push",
        data={"defaults": "stage"},
    )


def erratum_refresh_security_alerts(erratum_id, *, dry_run: bool = False):
    if dry_run:
        logger.info("Dry run: Would refresh security alerts for erratum %s", erratum_id)
        return

    ET_api_post(f"erratum/{erratum_id}/security_alerts/refresh", {})


def erratum_change_state(erratum_id, new_state: ErrataStatus, *, dry_run: bool = False):
    if dry_run:
        logger.info(
            "Dry run: Would change state of erratum %s to %s", erratum_id, new_state
        )
        return

    ET_api_post(
        f"erratum/{erratum_id}/change_state",
        data={"new_state": new_state},
    )


def erratum_change_ownership(
    erratum_id: int | str, new_owner_email: str, *, dry_run: bool = False
):
    if dry_run:
        logger.info(
            "Dry run: Would change the ownership of erratum %s to %s",
            erratum_id,
            new_owner_email,
        )
        return

    ET_api_put(
        f"erratum/{erratum_id}",
        {
            "advisory[assigned_to_email]": new_owner_email,
            "advisory[package_owner_email]": new_owner_email,
        },
    )


def erratum_add_comment(erratum_id: int | str, content: str, *, dry_run: bool = False):
    if dry_run:
        logger.info(
            "Dry run: Would add '%s' as a comment for erratum %s", content, erratum_id
        )
        return

    ET_api_post(f"erratum/{erratum_id}/add_comment", data={"comment": content})


if __name__ == "__main__":
    print(get_erratum_transition_rules(151838))
