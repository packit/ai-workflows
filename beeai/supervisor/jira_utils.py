from datetime import datetime
from enum import Enum, StrEnum
from functools import cache
import logging
import os
from typing import (
    Any,
    Collection,
    Generator,
    Literal,
    Type,
    TypeVar,
    overload,
)
import requests
from urllib.parse import quote as urlquote

from .supervisor_types import (
    FullIssue,
    Issue,
    IssueComment,
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
    response.raise_for_status()
    return {field["name"]: field["id"] for field in response.json()}


CURRENT_ISSUES_JQL = """
filter = Jotnar_1000_packages
AND status in ('In Progress', 'Integration', 'Release Pending')
AND 'Fixed in Build' is not EMPTY
"""


@overload
def decode_issue(issue_data: Any, full: Literal[False] = False) -> Issue: ...


@overload
def decode_issue(issue_data: Any, full: Literal[True]) -> FullIssue: ...


def decode_issue(issue_data: Any, full: bool = False) -> Issue | FullIssue:
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

    issue = Issue(
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

    if full:
        return FullIssue(
            **issue.__dict__,
            comments=[
                IssueComment(
                    authorName=c["author"]["displayName"],
                    authorEmail=c["author"]["emailAddress"],
                    created=datetime.fromisoformat(c["created"]),
                    body=c["body"],
                )
                for c in issue_data["fields"]["comment"]["comments"]
            ],
        )
    else:
        return issue


def _fields(full: bool):
    # Passing in the specific list of fields improves performance
    # significantly - in a test case, it reduced the time to fetch
    # 145 issues from 16s to 0.7s.

    custom_fields = get_custom_fields()
    base_fields = [
        "components",
        "summary",
        "status",
        "fixVersions",
        custom_fields["Errata Link"],
        custom_fields["Fixed in Build"],
        custom_fields["Test Coverage"],
        custom_fields["Preliminary Testing"],
    ]
    if full:
        return base_fields + ["comment"]
    else:
        return base_fields


@overload
def get_issue(issue_key: str, full: Literal[False] = False) -> Issue: ...


@overload
def get_issue(issue_key: str, full: Literal[True]) -> FullIssue: ...


def get_issue(issue_key: str, full: bool = False) -> Issue | FullIssue:
    url = f"https://issues.redhat.com/rest/api/2/issue/{urlquote(issue_key)}?fields={','.join(_fields(full))}"
    response = requests.get(url, headers=jira_headers())
    response.raise_for_status()
    issue_data = response.json()

    return decode_issue(issue_data, full)


@overload
def get_current_issues(
    full: Literal[False] = False,
) -> Generator[Issue, None, None]: ...


@overload
def get_current_issues(full: Literal[True]) -> Generator[FullIssue, None, None]: ...


def get_current_issues(
    full: bool = False,
) -> Generator[Issue, None, None] | Generator[FullIssue, None, None]:
    start_at = 0
    max_results = 1000
    while True:
        body = {
            "jql": CURRENT_ISSUES_JQL,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": _fields(full),
        }

        url = "https://issues.redhat.com/rest/api/2/search"
        logger.debug("Fetching JIRA issues, start=%d, max=%d", start_at, max_results)
        response = requests.post(url, json=body, headers=jira_headers())
        response_data = response.json()
        logger.debug("Got %d issues", len(response_data["issues"]))

        for issue_data in response_data["issues"]:
            yield decode_issue(issue_data, full)

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


class CommentVisibility(StrEnum):
    PUBLIC = "Public"
    RED_HAT_EMPLOYEE = "Red Hat Employee"


CommentSpec = None | str | tuple[str, CommentVisibility]


def _add_comment_update(
    update: dict[str, Any], comment: CommentSpec
) -> dict[str, Any] | None:
    if comment is None:
        return

    if isinstance(comment, str):
        comment_value = comment
        visibility = CommentVisibility.PUBLIC
    else:
        comment_value, visibility = comment

    if visibility == CommentVisibility.PUBLIC:
        comment_update = {"add": {"body": comment_value}}
    else:
        comment_update = {
            "add": {
                "body": comment_value,
                "visibility": {"type": "group", "value": str(visibility)},
            }
        }

    update["comment"] = [comment_update]


def change_issue_status(
    issue_key: str,
    new_status: IssueStatus,
    comment: CommentSpec = None,
    *,
    dry_run: bool = False,
) -> None:
    url = f"https://issues.redhat.com/rest/api/2/issue/{urlquote(issue_key)}/transitions?expand=transitions.fields"
    response = requests.get(url, headers=jira_headers())
    response.raise_for_status()

    status_str = str(new_status)
    transition = None
    for t in response.json()["transitions"]:
        if t["to"]["name"] == status_str:
            transition = t
            break

    if transition is None:
        raise ValueError(f"Cannot transition issue {issue_key} to status {status_str}")

    if any(f["required"] for f in transition.get("fields", {}).values()):
        raise ValueError(
            f"Cannot transition issue {issue_key} to status {status_str}: transition has required fields"
        )

    url = (
        f"https://issues.redhat.com/rest/api/2/issue/{urlquote(issue_key)}/transitions"
    )
    body: dict[str, Any] = {"transition": {"id": transition["id"]}, "update": {}}
    if comment is not None:
        _add_comment_update(body["update"], comment)

    if dry_run:
        logger.info(
            "Dry run: would change issue %s status to %s", issue_key, new_status
        )
        logger.debug("Dry run: would post %s to %s", body, url)
        return

    response = requests.post(url, json=body, headers=jira_headers())
    response.raise_for_status()


def add_issue_label(
    issue_key: str, label: str, comment: CommentSpec = None, *, dry_run: bool = False
) -> None:
    url = f"https://issues.redhat.com/rest/api/2/issue/{urlquote(issue_key)}"
    body: dict[str, Any] = {
        "update": {"labels": [{"add": label}]},
    }
    if comment is not None:
        _add_comment_update(body["update"], comment)

    if dry_run:
        logger.info("Dry run: would add label %s to issue %s", label, issue_key)
        logger.debug("Dry run: would post %s to %s", body, url)
        return

    response = requests.put(url, json=body, headers=jira_headers())
    response.raise_for_status()


if __name__ == "__main__":
    print(get_issue(os.environ["JIRA_ISSUE"], full=True).model_dump_json())
