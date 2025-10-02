from datetime import datetime, timezone
from enum import StrEnum
from datetime import datetime
from functools import cache
import logging
import os
from typing import overload
from typing_extensions import Literal

from bs4 import BeautifulSoup, Tag  # type: ignore
from pydantic import BaseModel
from requests_gssapi import HTTPSPNEGOAuth

from .http_utils import requests_session
from .supervisor_types import Erratum, FullErratum, ErrataStatus, Comment

logger = logging.getLogger(__name__)


ET_URL = "https://errata.engineering.redhat.com/"


@cache
def ET_verify() -> bool | str:
    verify = os.getenv("REDHAT_IT_CA_BUNDLE")
    if verify:
        return verify
    else:
        return True


def ET_api_get(path: str):
    response = requests_session().get(
        f"{ET_URL}/api/v1/{path}",
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
    )
    response.raise_for_status()
    return response.json()


def ET_api_post(path: str, data: dict):
    response = requests_session().post(
        f"{ET_URL}/api/v1/{path}",
        data=data,
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
    )
    response.raise_for_status()
    return response.json()


def ET_get_html(path: str):
    response = requests_session().get(
        f"{ET_URL}/{path}",
        auth=HTTPSPNEGOAuth(opportunistic_auth=True),
        verify=ET_verify(),
    )
    response.raise_for_status()
    return response.text


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

    jira_issues = data["jira_issues"]["jira_issues"]

    all_issues_release_pending = all(
        jira_issue_data["jira_issue"]["status"] == "Release Pending"
        for jira_issue_data in jira_issues
    )

    last_status_transition_timestamp = datetime.strptime(
        details["status_updated_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)

    base_erratum = Erratum(
        id=details["id"],
        full_advisory=details["fulladvisory"],
        url=f"https://errata.engineering.redhat.com/advisory/{erratum_id}",
        synopsis=details["synopsis"],
        status=ErrataStatus(details["status"]),
        all_issues_release_pending=all_issues_release_pending,
        last_status_transition_timestamp=last_status_transition_timestamp,
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


@overload
def get_erratum_for_link(link: str, full: Literal[False] = False) -> Erratum: ...


@overload
def get_erratum_for_link(link: str, full: Literal[True]) -> FullErratum: ...


def get_erratum_for_link(link: str, full: bool = True) -> Erratum | FullErratum:
    erratum_id = link.split("/")[-1]
    return get_erratum(erratum_id, full=full)


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


def erratum_get_latest_stage_push_status(erratum_id) -> ErratumPushStatus | None:
    pushes = ET_api_get(
        f"erratum/{erratum_id}/push",
    )

    highest_push_id = 0
    status = None
    for push in pushes:
        if push["target"]["name"] == "cdn_stage" and push["id"] > highest_push_id:
            highest_push_id = push["id"]
            status = push["status"]

    return ErratumPushStatus(status) if status else None


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


if __name__ == "__main__":
    print(get_erratum_transition_rules(151838))
