import backoff
import base64
from datetime import datetime
from enum import Enum, StrEnum
from functools import cache
from json import dumps as json_dumps
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
from urllib.parse import quote as urlquote

import requests

from .http_utils import requests_session
from .supervisor_types import (
    FullIssue,
    Issue,
    IssueStatus,
    JiraComment,
    JotnarTag,
    TestCoverage,
    PreliminaryTesting,
)


logger = logging.getLogger(__name__)

# Jira API support for both Jira cloud and server.
# uses jira API v2 by default, v3 only where v2 is deprecated.
# v2 returns plain text and is easier to parse, v3 returns ADF in complex JSON obj format.
# v2 works on both cloud and server, v3 only exists on cloud.
# v3 is used only for:
# - cloud's /search/jql endpoint
# - cloud's /user/search endpoint (requires 'query' param instead of 'username')

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
def jira_url() -> str:
    url = os.environ.get("JIRA_URL", "https://issues.redhat.com")
    return url.rstrip("/")


def is_jira_cloud() -> bool:
    """Returns True if connected to Jira Cloud (atlassian.net)."""
    return "atlassian.net" in jira_url()


class JiraNotLoggedInError(Exception):
    pass


@cache
def jira_headers() -> dict[str, str]:
    jira_token = os.environ["JIRA_TOKEN"]

    if is_jira_cloud():
        # Cloud: Basic Auth with email:token (required for both v2 and v3 endpoints)
        jira_email = os.environ.get("JIRA_EMAIL")
        if not jira_email:
            raise ValueError("JIRA_EMAIL environment variable is required for Jira Cloud")

        auth_value = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
        auth_header = f"Basic {auth_value}"
    else:
        # Server: Bearer token
        auth_header = f"Bearer {jira_token}"

    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }

    # Test if the token can log in successfully
    response = requests_session().get(
        f"{jira_url()}/rest/api/2/myself", headers=headers
    )

    if response.status_code == 401:
        raise JiraNotLoggedInError(
            "Jira login authentication failed. Please check if the Jira token is valid."
        )

    response.raise_for_status()

    return headers


class RateLimitError(Exception):
    pass


def raise_for_status(response: requests.Response) -> None:
    if response.status_code == 429:
        # JIRA sets a Retry-After header, but at least for JIRA Server
        # it appears always to be 0s, which is not very useful, so
        # we ignore it and use the backoff library to handle it.
        raise RateLimitError()
    response.raise_for_status()


# Define a custom decorator for our specific retry policy. You might
# thing you could do retry-on_rate_limit = backoff.on_exception(...)
# but that doesn't because of implementation details of backoff.
# See: https://github.com/litl/backoff/issues/179


def retry_on_rate_limit(func):
    return backoff.on_exception(
        backoff.expo,
        RateLimitError,
        max_time=300,
        jitter=backoff.full_jitter,
    )(func)


@retry_on_rate_limit
def jira_api_get(path: str, *, params: dict | None = None, api_version: Literal["2", "3"] = "2") -> Any:
    version = api_version  #defaults to v2 for plain text
    url = f"{jira_url()}/rest/api/{version}/{path}"
    response = requests_session().get(url, headers=jira_headers(), params=params)
    if not response.ok:
        logger.error(
            "GET %s%s failed.\nerror:\n%s",
            url,
            f" (params={params})" if params else "",
            response.text,
        )
    raise_for_status(response)
    return response.json()


@overload
def jira_api_post(
    path: str, json: dict[str, Any], *, decode_response: Literal[False] = False
) -> None: ...


@overload
def jira_api_post(
    path: str, json: dict[str, Any], *, decode_response: Literal[True]
) -> Any: ...


@retry_on_rate_limit
def jira_api_post(
    path: str, json: dict[str, Any], *, decode_response: bool = False, api_version: Literal["2", "3"] = "2"
) -> Any | None:
    version = api_version  #defaults to v2 for plain text
    url = f"{jira_url()}/rest/api/{version}/{path}"
    response = requests_session().post(url, headers=jira_headers(), json=json)
    if not response.ok:
        logger.error(
            "POST to %s failed\nbody:\n%s\nerror:\n%s",
            url,
            json_dumps(json, indent=2),
            response.text,
        )
    raise_for_status(response)
    if decode_response:
        return response.json()


@overload
def jira_api_upload(
    path: str,
    attachments: list[tuple[str, bytes, str]],
    *,
    decode_response: Literal[False] = False,
) -> None: ...


@overload
def jira_api_upload(
    path: str,
    attachments: list[tuple[str, bytes, str]],
    *,
    decode_response: Literal[True],
) -> Any: ...


@retry_on_rate_limit
def jira_api_upload(
    path: str,
    attachments: list[tuple[str, bytes, str]],
    *,
    decode_response: bool = False,
) -> Any | None:
    url = f"{jira_url()}/rest/api/2/{path}"  #use v2 for uploads
    files = [("file", a) for a in attachments]
    headers = dict(jira_headers())
    del headers["Content-Type"]  # requests will set this correctly for multipart
    headers["X-Atlassian-Token"] = "no-check"
    response = requests_session().post(url, headers=headers, files=files)
    if not response.ok:
        logger.error(
            "POST of %s to %s failed\n\nerror:\n%s",
            ", ".join(filename for filename, _, _ in attachments),
            url,
            response.text,
        )
    raise_for_status(response)
    if decode_response:
        return response.json()


@overload
def jira_api_put(
    path: str, json: dict[str, Any], *, decode_response: Literal[False] = False
) -> None: ...


@overload
def jira_api_put(
    path: str, json: dict[str, Any], *, decode_response: Literal[True]
) -> Any: ...


@retry_on_rate_limit
def jira_api_put(
    path: str, json: dict[str, Any], *, decode_response: bool = False, api_version: Literal["2", "3"] = "2"
) -> Any | None:
    version = api_version  # Default to v2 for plain text compatibility
    url = f"{jira_url()}/rest/api/{version}/{path}"
    response = requests_session().put(url, headers=jira_headers(), json=json)
    if not response.ok:
        logger.error(
            "PUT to %s failed\nbody:\n%s\nerror:\n%s",
            url,
            json_dumps(json, indent=2),
            response.text,
        )
    raise_for_status(response)
    if decode_response:
        return response.json()


@cache
def get_custom_fields() -> dict[str, str]:
    response = jira_api_get("field")
    return {field["name"]: field["id"] for field in response}


@overload
def decode_issue(issue_data: Any, full: Literal[False] = False) -> Issue: ...


@overload
def decode_issue(issue_data: Any, full: Literal[True]) -> FullIssue: ...


def decode_issue(issue_data: Any, full: bool = False) -> Issue | FullIssue:
    custom_fields = get_custom_fields()

    _E = TypeVar("_E", bound=Enum)

    def custom(name) -> Any | None:
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
    assigned_team = custom("AssignedTeam")
    assigned_team_name = assigned_team.get("value") if assigned_team else None

    issue = Issue(
        key=key,
        url=f"https://issues.redhat.com/browse/{urlquote(key)}",
        assigned_team=assigned_team_name,
        summary=issue_data["fields"]["summary"],
        status=issue_data["fields"]["status"]["name"],
        components=issue_components,
        labels=issue_data["fields"]["labels"],
        fix_versions=[v["name"] for v in issue_data["fields"]["fixVersions"]],
        errata_link=errata_link,
        fixed_in_build=custom("Fixed in Build"),
        test_coverage=custom_enum_list(TestCoverage, "Test Coverage"),
        preliminary_testing=custom_enum(PreliminaryTesting, "Preliminary Testing"),
    )

    if full:
        return FullIssue(
            **issue.__dict__,
            description=issue_data["fields"]["description"] or "",
            comments=[
                JiraComment(
                    authorName=c["author"]["displayName"],
                    authorEmail=c["author"].get("emailAddress"),
                    created=datetime.fromisoformat(c["created"]),
                    body=c["body"],
                    id=c["id"],
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
        "labels",
        "summary",
        "status",
        "fixVersions",
        custom_fields["AssignedTeam"],
        custom_fields["Errata Link"],
        custom_fields["Fixed in Build"],
        custom_fields["Test Coverage"],
        custom_fields["Preliminary Testing"],
    ]
    if full:
        return base_fields + ["comment", "description"]
    else:
        return base_fields


@overload
def get_issue(issue_key: str, full: Literal[False] = False) -> Issue: ...


@overload
def get_issue(issue_key: str, full: Literal[True]) -> FullIssue: ...


def get_issue(issue_key: str, full: bool = False) -> Issue | FullIssue:
    path = f"issue/{urlquote(issue_key)}?fields={','.join(_fields(full))}"
    # Passing fields using the params dict caused the response time to increase;
    # perhaps the JIRA server isn't properly decoding encoded `,` characters and ignoring
    # fields, so we build the URL ourselves
    response_data = jira_api_get(path)
    return decode_issue(response_data, full)


@overload
def get_current_issues(
    jql: str,
    full: Literal[False] = False,
) -> Generator[Issue, None, None]: ...


@overload
def get_current_issues(
    jql: str, full: Literal[True]
) -> Generator[FullIssue, None, None]: ...


def get_current_issues(
    jql: str,
    full: bool = False,
) -> Generator[Issue, None, None] | Generator[FullIssue, None, None]:
    max_results = 1000

    if is_jira_cloud():
        # Cloud: Use v3 search/jql endpoint (v2 is deprecated)
        next_page_token = None
        while True:
            body = {
                "jql": jql,
                "maxResults": max_results,
                # when full=True, just fetch issue key (will re-fetch full issue with v2)
                "fields": [] if full else _fields(False),
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token

            logger.debug("Fetching JIRA issues (v3), token=%s, max=%d", next_page_token, max_results)
            response_data = jira_api_post("search/jql", json=body, decode_response=True, api_version="3")
            logger.debug("Got %d issues", len(response_data["issues"]))

            for issue_data in response_data["issues"]:
                if full:
                    # Fetch full issue with v2 to get plain text descriptions/comments
                    issue_key = issue_data["key"]
                    yield get_issue(issue_key, full=True)
                else:
                    yield decode_issue(issue_data, full=False)

            if response_data.get("isLast", True):
                break
            next_page_token = response_data.get("nextPageToken")
    else:
        # Server: Use v2 search endpoint
        start_at = 0
        while True:
            body = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": _fields(full),
            }
            logger.debug("Fetching JIRA issues (v2), start=%d, max=%d", start_at, max_results)
            response_data = jira_api_post("search", json=body, decode_response=True)
            logger.debug("Got %d issues", len(response_data["issues"]))

            for issue_data in response_data["issues"]:
                yield decode_issue(issue_data, full)

            if response_data["total"] <= start_at + max_results:
                break
            start_at += max_results


@overload
def get_issue_by_jotnar_tag(
    project: str,
    tag: JotnarTag,
    full: Literal[False] = False,
    with_label: str | None = None,
) -> Issue | None: ...


@overload
def get_issue_by_jotnar_tag(
    project: str,
    tag: JotnarTag,
    full: Literal[True],
    with_label: str | None = None,
) -> FullIssue | None: ...


def get_issue_by_jotnar_tag(
    project: str, tag: JotnarTag, full: bool = False, with_label: str | None = None
) -> Issue | FullIssue | None:
    max_results = 2
    jql = f'project = {project} AND status NOT IN (Done, Closed) AND description ~ "\\"{tag}\\""'
    if with_label is not None:
        jql += f' AND labels = "{with_label}"'

    if is_jira_cloud():
        # Cloud: Use v3 search/jql endpoint (v2 is deprecated)
        body = {
            "jql": jql,
            "maxResults": max_results,
            # when full=True, just fetch issue key (will re-fetch full issue with v2)
            "fields": [] if full else _fields(False),
        }
        logger.debug("Fetching JIRA issues (v3), max=%d", max_results)
        response_data = jira_api_post("search/jql", json=body, decode_response=True, api_version="3")
    else:
        # Server: Use v2 search endpoint
        body = {
            "jql": jql,
            "startAt": 0,
            "maxResults": max_results,
            "fields": _fields(full),
        }
        logger.debug("Fetching JIRA issues (v2), start=0, max=%d", max_results)
        response_data = jira_api_post("search", json=body, decode_response=True)

    if len(response_data["issues"]) == 0:
        return None
    elif len(response_data["issues"]) > 1:
        raise ValueError(f"Multiple open issues found with JOTNAR tag {tag}")
    else:
        issue_key = response_data["issues"][0]["key"]
        if is_jira_cloud() and full:
            # Cloud: Fetch full issue with v2 to get plain text
            return get_issue(issue_key, full=True)
        else:
            return decode_issue(response_data["issues"][0], full)


def get_issues_statuses(issue_keys: Collection[str]) -> dict[str, IssueStatus]:
    if len(issue_keys) == 0:
        return {}

    jql = f"key in ({','.join(issue_keys)})"
    body = {
        "jql": jql,
        "maxResults": len(issue_keys),
        "fields": ["status"],
    }
    if is_jira_cloud():
        # Cloud: Use v3 search/jql endpoint (v2 is deprecated)
        response_data = jira_api_post("search/jql", json=body, decode_response=True, api_version="3")
    else:
        # Server: Use v2 search endpoint
        response_data = jira_api_post("search", json=body, decode_response=True)

    result = {
        issue_data["key"]: IssueStatus(issue_data["fields"]["status"]["name"])
        for issue_data in response_data["issues"]
    }

    missing_keys = set(issue_keys) - result.keys()
    if missing_keys:
        raise KeyError(f"Can't find issues in JIRA: {', '.join(missing_keys)}")

    return result


class CommentVisibility(StrEnum):
    PUBLIC = "Public"
    RED_HAT_EMPLOYEE = "Red Hat Employee"


CommentSpec = None | str | tuple[str, CommentVisibility]


def _comment_to_dict(comment: CommentSpec) -> dict[str, Any] | None:
    if comment is None:
        return

    if isinstance(comment, str):
        comment_value = comment
        visibility = CommentVisibility.PUBLIC
    else:
        comment_value, visibility = comment

    # v2 API uses plain text for comments
    if visibility == CommentVisibility.PUBLIC:
        return {"body": comment_value}
    else:
        return {
            "body": comment_value,
            "visibility": {"type": "group", "value": str(visibility)},
        }


def _add_comment_update(update: dict[str, Any], comment: CommentSpec) -> None:
    comment_dict = _comment_to_dict(comment)
    if comment_dict is None:
        return

    update["comment"] = [{"add": comment_dict}]


def add_issue_comment(
    issue_key: str, comment: CommentSpec, *, dry_run: bool = False
) -> None:
    body = _comment_to_dict(comment)
    if body is None:
        return

    path = f"issue/{urlquote(issue_key)}/comment"
    if dry_run:
        logger.info("Dry run: would add comment to issue %s: %s", issue_key, comment)
        logger.debug("Dry run: would post %s to %s", body, path)
        return

    jira_api_post(path, json=body)


def update_issue_comment(
    issue_key: str, comment_id: str, comment: CommentSpec, *, dry_run: bool = False
) -> None:
    body = _comment_to_dict(comment)
    if body is None:
        return

    path = f"issue/{urlquote(issue_key)}/comment/{urlquote(comment_id)}"
    if dry_run:
        logger.info(
            "Dry run: would update comment %s on issue %s: %s",
            comment_id,
            issue_key,
            comment,
        )
        logger.debug("Dry run: would put %s to %s", body, path)
        return

    jira_api_put(path, json=body)


def change_issue_status(
    issue_key: str,
    new_status: IssueStatus,
    comment: CommentSpec = None,
    *,
    dry_run: bool = False,
) -> None:
    path = f"issue/{urlquote(issue_key)}/transitions"
    response_data = jira_api_get(path, params={"expand": "transitions.fields"})

    status_str = str(new_status)
    transition = None
    for t in response_data["transitions"]:
        if t["to"]["name"] == status_str:
            transition = t
            break

    if transition is None:
        raise ValueError(f"Cannot transition issue {issue_key} to status {status_str}")

    if any(f["required"] for f in transition.get("fields", {}).values()):
        raise ValueError(
            f"Cannot transition issue {issue_key} to status {status_str}: transition has required fields"
        )

    path = f"issue/{urlquote(issue_key)}/transitions"
    body: dict[str, Any] = {"transition": {"id": transition["id"]}, "update": {}}

    can_transition_with_comment = "comment" in transition.get("fields", {})

    if comment is not None and can_transition_with_comment:
        _add_comment_update(body["update"], comment)

    if dry_run:
        logger.info(
            "Dry run: would change issue %s status to %s", issue_key, new_status
        )
        logger.debug("Dry run: would post %s to %s", body, path)

    if not dry_run:
        jira_api_post(path, json=body)

    if comment is not None and not can_transition_with_comment:
        add_issue_comment(issue_key, comment, dry_run=dry_run)


def format_attention_message(why: str) -> str:
    return (
        "{panel:title=Project Jötnar: ATTENTION NEEDED|"
        "borderStyle=solid|borderColor=#CC0000|titleBGColor=#FFF5F5|bgColor=#FFFEF0}\n"
        f"{why}\n\n"
        "Please resolve this and remove the {{jotnar_needs_attention}} flag.\n"
        "{panel}"
    )


def add_issue_label(
    issue_key: str, label: str, comment: CommentSpec = None, *, dry_run: bool = False
) -> None:
    path = f"issue/{urlquote(issue_key)}"
    body: dict[str, Any] = {
        "update": {"labels": [{"add": label}]},
    }
    if comment is not None:
        _add_comment_update(body["update"], comment)

    if dry_run:
        logger.info("Dry run: would add label %s to issue %s", label, issue_key)
        logger.debug("Dry run: would post %s to %s", body, path)
        return

    jira_api_put(path, json=body)


def remove_issue_label(
    issue_key: str, label: str, comment: CommentSpec = None, *, dry_run: bool = False
) -> None:
    path = f"issue/{urlquote(issue_key)}"
    body: dict[str, Any] = {
        "update": {"labels": [{"remove": label}]},
    }
    if comment is not None:
        _add_comment_update(body["update"], comment)

    if dry_run:
        logger.info("Dry run: would remove label %s from issue %s", label, issue_key)
        logger.debug("Dry run: would post %s to %s", body, path)
        return

    jira_api_put(path, json=body)


def add_issue_attachments(
    issue_key: str,
    attachments: list[tuple[str, bytes, str]],
    *,
    comment: CommentSpec = None,
    dry_run: bool = False,
) -> None:
    """
    Adds attachments to a JIRA issue.

    Args:
        issue_key: The key of the issue to add attachments to.
        attachments: A list of tuples of (filename, file bytes, mime type).
        comment: An optional comment to add to the issue along with the attachments.
        dry_run: If True, don't actually add the attachments.
    """
    path = f"issue/{urlquote(issue_key)}/attachments"

    if len(attachments) == 0:
        # This an API error; don't try to call it.
        logger.info("No attachments to add to issue %s", issue_key)
        return

    if dry_run:
        logger.info(
            "Dry run: would add attachment(s) %s to issue %s",
            ", ".join(filename for filename, _, _ in attachments),
            issue_key,
        )
        logger.debug("Dry run: would post attachment to %s", path)
        return

    jira_api_upload(path, attachments=attachments)

    # The result is a list of attachment metadata dicts, each with an "id" field.
    # But we can't use that ID for anything useful - in comments we can only
    # reference attachments by filename, not ID.

    if comment is not None:
        add_issue_comment(issue_key, comment, dry_run=dry_run)


def get_issue_attachment(issue_key: str, filename: str) -> bytes:
    """
    Retrieve the content of a specific attachment from a JIRA issue.

    Args:
        issue_key: The key of the JIRA issue.
        filename: The name of the attachment file to retrieve.

    Returns:
        The content of the attachment as bytes.

    Raises:
        KeyError: If the attachment with the specified filename is not found
           or if multiple attachments with the same filename exist.
    """
    path = f"issue/{urlquote(issue_key)}?fields=attachment"
    attachments = jira_api_get(path)["fields"]["attachment"]

    attachments = [a for a in attachments if a["filename"] == filename]
    if len(attachments) == 0:
        raise KeyError(f"Issue {issue_key} has no attachment named {filename}")
    if len(attachments) > 1:
        raise KeyError(f"Issue {issue_key} has multiple attachments named {filename}")

    url = attachments[0]["content"]
    response = requests_session().get(url, headers=jira_headers())
    raise_for_status(response)
    return response.content


@cache
def get_user_name(email: str) -> str:
    # Cloud: v3 uses 'query' parameter; Server: v2 uses 'username' parameter
    if is_jira_cloud():
        users = jira_api_get("user/search", params={"query": email}, api_version="3")
    else:
        users = jira_api_get("user/search", params={"username": email})

    if len(users) == 0:
        raise ValueError(f"No JIRA user with email {email}")
    elif len(users) > 1:
        raise ValueError(f"Multiple JIRA users with email {email}")

    user = users[0]

    return user.get("name") or user.get("displayName") or user["accountId"]


@overload
def create_issue(
    *,
    project: str,
    summary: str,
    description: str,
    tag: JotnarTag | None = None,
    assignee_email: str | None = None,
    reporter_email: str | None = None,
    components: Collection[str] | None = None,
    fix_versions: Collection[str] | None = None,
    labels: Collection[str] | None = None,
    dry_run: Literal[False] = False,
) -> str: ...


@overload
def create_issue(
    *,
    project: str,
    summary: str,
    description: str,
    tag: JotnarTag | None = None,
    assignee_email: str | None = None,
    reporter_email: str | None = None,
    components: Collection[str] | None = None,
    fix_versions: Collection[str] | None = None,
    labels: Collection[str] | None = None,
    dry_run: Literal[True],
) -> None: ...


def create_issue(
    *,
    project: str,
    summary: str,
    description: str,
    tag: JotnarTag | None = None,
    assignee_email: str | None = None,
    reporter_email: str | None = None,
    components: Collection[str] | None = None,
    fix_versions: Collection[str] | None = None,
    labels: Collection[str] | None = None,
    dry_run: bool = False,
) -> str | None:
    if tag is not None:
        description = f"{tag}\n\n{description}"


    fields = {
        "project": {"key": project},
        "summary": summary,
        "description": description,
        "issuetype": {"name": "Task"},
    }

    if assignee_email:
        fields |= {"assignee": {"name": get_user_name(assignee_email)}}

    if reporter_email:
        fields |= {"reporter": {"name": get_user_name(reporter_email)}}

    if components:
        fields |= {"components": [{"name": c} for c in components]}

    if fix_versions:
        fields |= {"fixVersions": [{"name": v} for v in fix_versions]}

    if labels:
        fields |= {"labels": list(labels)}

    path = "issue"
    body = {"fields": fields}

    if dry_run:
        logger.info(
            "Dry run: would add file new issue project=%s, summary=%s, jotnar_tag=%s",
            project,
            summary,
            tag,
        )
        logger.debug("Dry run: would post %s to %s", body, path)
        return

    response_data = jira_api_post(path, json=body, decode_response=True)
    key = response_data["key"]
    logger.info("Created new issue %s", key)

    return key


if __name__ == "__main__":
    import asyncio
    from .http_utils import with_requests_session

    @with_requests_session()
    async def main():
        logging.basicConfig(level=logging.DEBUG)
        print(get_issue(os.environ["JIRA_ISSUE"], full=True).model_dump_json())

    asyncio.run(main())
