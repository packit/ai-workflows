from enum import Enum
from functools import cache
import logging
import os
from typing import Any, Collection, Generator, Type, TypeVar
import requests
from urllib.parse import quote as urlquote

from .supervisor_types import (
    Issue,
    IssueStatus,
    TestCoverage,
    PreliminaryTesting,
)


logger = logging.getLogger(__name__)


@cache
def components():
    result: list[str] = []
    with open("components.csv") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#"):
                continue
            component, _ = line.strip().split(",")
            result.append(component)

    return result


def quote(component: str):
    return f"'{component}'"


@cache
def jira_headers() -> dict[str, str]:
    jira_token = os.environ["JIRA_TOKEN"]

    return {
        "Authorization": f"Bearer {jira_token}",
        "Content-Type": "application/json",
    }


@cache
def get_custom_fields() -> dict[str, str]:
    URL = "https://issues.redhat.com/rest/api/2/field"
    response = requests.get(URL, headers=jira_headers())
    return {field["name"]: field["id"] for field in response.json()}


CURRENT_ISSUES_JQL = """
filter = Jotnar_1000_packages
AND status in ('In Progress', 'Integration', 'Release Pending')
AND 'Fixed in Build' is not EMPTY
"""


def decode_issue(issue_data: Any) -> Issue:
    custom_fields = get_custom_fields()

    _E = TypeVar("_E", bound=Enum)

    def custom(name) -> str | None:
        return issue_data["fields"].get(custom_fields[name])

    def custom_enum(enum_class: Type[_E], name) -> _E | None:
        data = issue_data["fields"].get(custom_fields[name])
        if data is None:
            return None
        else:
            return enum_class(data["value"])

    def custom_enum_list(enum_class: Type[_E], name) -> list[_E] | None:
        data = issue_data["fields"].get(custom_fields[name])
        if data is None:
            return None
        else:
            return [enum_class(d["value"]) for d in data]

    key = issue_data["key"]
    issue_components: list[str] = [
        str(v["name"]) for v in issue_data["fields"]["components"]
    ]
    errata_link = custom("Errata Link")

    return Issue(
        key=key,
        url=f"https://issues.redhat.com/browse/{urlquote(key)}",
        summary=issue_data["fields"]["summary"],
        status=issue_data["fields"]["status"]["name"],
        components=issue_components,
        fix_versions=[v["name"] for v in issue_data["fields"]["fixVersions"]],
        errata_link=errata_link,
        fixed_in_build=custom("Fixed in Build"),
        test_coverage=custom_enum_list(TestCoverage, "Test Coverage"),
        preliminary_testing=custom_enum(PreliminaryTesting, "Preliminary Testing"),
    )


def _fields():
    # Passing in the specific list of fields improves performance
    # significantly - in a test case, it reduced the time to fetch
    # 145 issues from 16s to 0.7s.

    custom_fields = get_custom_fields()
    return [
        "components",
        "summary",
        "status",
        "fixVersions",
        custom_fields["Errata Link"],
        custom_fields["Fixed in Build"],
        custom_fields["Test Coverage"],
        custom_fields["Preliminary Testing"],
    ]


def get_issue(issue_key) -> Issue:
    url = f"https://issues.redhat.com/rest/api/2/issue/{urlquote(issue_key)}?fields={','.join(_fields())}"
    response = requests.get(url, headers=jira_headers())
    response.raise_for_status()
    issue_data = response.json()

    return decode_issue(issue_data)


def get_current_issues() -> Generator[Issue, None, None]:
    start_at = 0
    max_results = 1000
    while True:
        body = {
            "jql": CURRENT_ISSUES_JQL,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": _fields(),
        }

        url = "https://issues.redhat.com/rest/api/2/search"
        logger.debug("Fetching JIRA issues, start=%d, max=%d", start_at, max_results)
        response = requests.post(url, json=body, headers=jira_headers())
        response_data = response.json()
        logger.debug("Got %d issues", len(response_data["issues"]))

        for issue_data in response_data["issues"]:
            yield decode_issue(issue_data)

        start_at += max_results
        if response_data["total"] <= start_at:
            break


def get_issues_statuses(issue_keys: Collection[str]) -> dict[str, IssueStatus]:
    if len(issue_keys) == 0:
        return {}

    body = {
        "jql": f"key in ({','.join(issue_keys)})",
        "maxResults": len(issue_keys),
        "fields": ["status"],
    }

    url = "https://issues.redhat.com/rest/api/2/search"
    response = requests.post(url, json=body, headers=jira_headers())
    response_data = response.json()

    return {
        issue_data["key"]: IssueStatus(issue_data["fields"]["status"]["name"])
        for issue_data in response_data["issues"]
    }
