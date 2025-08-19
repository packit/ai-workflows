from enum import StrEnum
from functools import cache
import logging

from bs4 import BeautifulSoup, Tag  # type: ignore
from pydantic import BaseModel
import requests
from requests_gssapi import HTTPSPNEGOAuth

from .supervisor_types import Erratum, ErrataStatus

logger = logging.getLogger(__name__)


def get_erratum(erratum_id: str | int):
    logger.debug("Getting detailed information for erratum %s", erratum_id)
    data = requests.get(
        f"https://errata.engineering.redhat.com/api/v1/erratum/{erratum_id}",
        auth=HTTPSPNEGOAuth(),
    ).json()
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

    return Erratum(
        id=details["id"],
        full_advisory=details["fulladvisory"],
        url=f"https://errata.engineering.redhat.com/advisory/{erratum_id}",
        synopsis=details["synopsis"],
        status=ErrataStatus(details["status"]),
        all_issues_release_pending=all_issues_release_pending,
    )


def get_erratum_for_link(link: str):
    erratum_id = link.split("/")[-1]
    return get_erratum(erratum_id)


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
    html = requests.get(
        f"https://errata.engineering.redhat.com/workflow_rules/for_advisory/{erratum_id}",
        auth=HTTPSPNEGOAuth(),
    ).text
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


if __name__ == "__main__":
    print(get_erratum_transition_rules(151838))
