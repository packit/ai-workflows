"""Shared Jira REST client and sweep issue decoding."""

import logging
import os
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from urllib.parse import quote, urljoin

import backoff

from ymir.common.base_utils import get_jira_auth_headers
from ymir.common.http_utils import aiohttp_get_with_retries, with_aiohttp_session
from ymir.common.models import (
    FullIssue,
    Issue,
    IssueStatus,
    JiraComment,
    PreliminaryTesting,
    TestCoverage,
)

logger = logging.getLogger(__name__)
_custom_fields_by_url: dict[str, dict[str, str]] = {}


class JiraRateLimitError(Exception):
    """Raised for Jira 429 responses so the request can be retried."""


async def _jira_request_with_session(
    method: str,
    url: str,
    *,
    session,
    params: dict | None = None,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    decode_response: bool = False,
) -> Any | None:
    """Make one Jira request; 429 handling is supplied by the retry wrapper."""
    kwargs = {
        key: value
        for key, value in {"params": params, "json": json, "headers": headers}.items()
        if value is not None
    }

    if method.lower() == "get":
        get_kwargs = {key: value for key, value in kwargs.items() if key != "json"}
        async with aiohttp_get_with_retries(
            session,
            url,
            yield_final_retryable_response=True,
            **get_kwargs,
        ) as response:
            if response.status == 429:
                # Jira Server often sends Retry-After: 0, so retain the supervisor
                # client's exponential backoff instead of trusting that header.
                raise JiraRateLimitError(f"Jira rate limited {method.upper()} {url}")
            response.raise_for_status()
            if decode_response:
                return await response.json()
        return None

    request = getattr(session, method.lower())
    async with request(url, **kwargs) as response:
        if response.status == 429:
            # Jira Server often sends Retry-After: 0, so retain the supervisor
            # client's exponential backoff instead of trusting that header.
            raise JiraRateLimitError(f"Jira rate limited {method.upper()} {url}")
        response.raise_for_status()
        if decode_response:
            return await response.json()
    return None


@backoff.on_exception(
    backoff.expo,
    JiraRateLimitError,
    max_time=300,
    jitter=backoff.full_jitter,
)
async def _jira_api_request(
    method: str,
    url: str,
    *,
    session,
    params: dict | None = None,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    decode_response: bool = False,
) -> Any | None:
    """Retry Jira 429s using the supervisor client's 300-second backoff."""
    return await _jira_request_with_session(
        method,
        url,
        session=session,
        params=params,
        json=json,
        headers=headers,
        decode_response=decode_response,
    )


async def _jira_api_request_in_session(
    method: str,
    url: str,
    *,
    session=None,
    params: dict | None = None,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    decode_response: bool = False,
) -> Any | None:
    """Keep one session open across all retries for a Jira request."""
    if session is None:
        async with with_aiohttp_session() as current:
            return await _jira_api_request(
                method,
                url,
                session=current,
                params=params,
                json=json,
                headers=headers,
                decode_response=decode_response,
            )
    return await _jira_api_request(
        method,
        url,
        session=session,
        params=params,
        json=json,
        headers=headers,
        decode_response=decode_response,
    )


def jira_url() -> str:
    """Return the configured Jira URL, retaining the supervisor default."""
    return os.environ.get("JIRA_URL", "https://redhat.atlassian.net").rstrip("/")


def jira_api_url(path: str, *, api_version: Literal["2", "3"] = "2", base_url: str | None = None) -> str:
    """Build a Jira REST API URL from a relative path and API version."""
    base = (base_url or jira_url()).rstrip("/")
    return urljoin(f"{base}/", f"rest/api/{api_version}/{path.lstrip('/')}")


async def jira_api_get(
    path: str,
    *,
    params: dict | None = None,
    api_version: Literal["2", "3"] = "2",
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    session=None,
) -> Any:
    """GET JSON from Jira, retrying rate limits with the supervisor policy."""
    url = jira_api_url(path, api_version=api_version, base_url=base_url)
    return await _jira_api_request_in_session(
        "get",
        url,
        params=params,
        headers=headers or get_jira_auth_headers(),
        session=session,
        decode_response=True,
    )


async def jira_api_post(
    path: str,
    *,
    json: dict[str, Any],
    api_version: Literal["2", "3"] = "2",
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    session=None,
    decode_response: bool = False,
) -> Any | None:
    """POST JSON to Jira and optionally decode the response body."""
    url = jira_api_url(path, api_version=api_version, base_url=base_url)
    return await _jira_api_request_in_session(
        "post",
        url,
        json=json,
        headers=headers or get_jira_auth_headers(),
        session=session,
        decode_response=decode_response,
    )


async def jira_api_put(
    path: str,
    *,
    json: dict[str, Any],
    api_version: Literal["2", "3"] = "2",
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    session=None,
    decode_response: bool = False,
) -> Any | None:
    """PUT JSON to Jira and optionally decode the response body."""
    url = jira_api_url(path, api_version=api_version, base_url=base_url)
    return await _jira_api_request_in_session(
        "put",
        url,
        json=json,
        headers=headers or get_jira_auth_headers(),
        session=session,
        decode_response=decode_response,
    )


async def get_custom_fields() -> dict[str, str]:
    """Fetch Jira custom field name-to-ID mappings, caching them per base URL."""
    base = jira_url()
    if base not in _custom_fields_by_url:
        data = await jira_api_get("field")
        _custom_fields_by_url[base] = {field["name"]: field["id"] for field in data}
    return _custom_fields_by_url[base]


def _fields(custom_fields: dict[str, str], full: bool) -> list[str]:
    """Select Jira issue fields, including comments and description when full."""
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
    return [*base_fields, "comment", "description"] if full else base_fields


def decode_issue(
    issue_data: dict[str, Any],
    custom_fields: dict[str, str],
    full: bool = False,
) -> Issue | FullIssue:
    """Decode a Jira issue payload into the shared common model types."""

    def custom(name: str) -> Any | None:
        return issue_data["fields"].get(custom_fields[name])

    def custom_enum(enum_class: type[Enum], name: str) -> Enum | None:
        data = custom(name)
        return None if data is None else enum_class(data["value"])

    def custom_enum_list(enum_class: type[Enum], name: str) -> list[Enum] | None:
        data = custom(name)
        return None if data is None else [enum_class(item["value"]) for item in data]

    key = issue_data["key"]
    fields = issue_data["fields"]
    assigned_team = custom("AssignedTeam")
    issue = Issue(
        key=key,
        url=f"{jira_url()}/browse/{quote(key)}",
        assigned_team=assigned_team.get("value") if assigned_team else None,
        summary=fields["summary"],
        status=IssueStatus(fields["status"]["name"]),
        components=[str(value["name"]) for value in fields["components"]],
        labels=fields["labels"],
        fix_versions=[value["name"] for value in fields["fixVersions"]],
        errata_link=custom("Errata Link"),
        fixed_in_build=custom("Fixed in Build"),
        test_coverage=custom_enum_list(TestCoverage, "Test Coverage"),
        preliminary_testing=custom_enum(PreliminaryTesting, "Preliminary Testing"),
    )
    if not full:
        return issue
    return FullIssue(
        **issue.model_dump(),
        description=fields["description"] or "",
        comments=[
            JiraComment(
                authorName=comment["author"]["displayName"],
                authorEmail=comment["author"].get("emailAddress"),
                created=datetime.fromisoformat(comment["created"]),
                body=comment["body"],
                id=comment["id"],
            )
            for comment in fields["comment"]["comments"]
        ],
    )


async def get_issue(issue_key: str, full: bool = False) -> Issue | FullIssue:
    """Fetch and decode one Jira issue, optionally including full text fields."""
    custom_fields = await get_custom_fields()
    fields = ",".join(_fields(custom_fields, full))
    path = f"issue/{quote(issue_key)}?fields={fields}"
    data = await jira_api_get(path, api_version="2")
    return decode_issue(data, custom_fields, full)


async def get_current_issues(jql: str, full: bool = False) -> list[Issue] | list[FullIssue]:
    """Fetch every page of a Jira JQL search and decode its issues.

    Full issue requests refetch each result to include comments and
    description.

    Args:
        jql: Jira query used by the paginated search endpoint.
        full: Refetch each result as a ``FullIssue`` when true; otherwise
            decode the search results as ``Issue`` objects.

    Returns:
        Decoded issues from every page, in search result order.
    """
    custom_fields = await get_custom_fields()
    issues: list[Issue] | list[FullIssue] = []
    token = None
    while True:
        body: dict[str, Any] = {
            "jql": jql,
            "maxResults": 1000,
            "fields": [] if full else _fields(custom_fields, False),
        }
        if token:
            body["nextPageToken"] = token
        page = await jira_api_post("search/jql", json=body, api_version="3", decode_response=True)
        for item in page["issues"]:
            decoded = await get_issue(item["key"], full=True) if full else decode_issue(item, custom_fields)
            issues.append(decoded)
        token = page.get("nextPageToken")
        if not token or page.get("isLast", True):
            break
    return issues


async def update_issue_label(
    issue_key: str,
    label: str,
    *,
    add: bool,
    comment: str | None = None,
    session=None,
) -> None:
    """Add or remove a Jira label, optionally adding a comment in the same update.

    Args:
        issue_key: Jira issue to update.
        label: Label to add or remove.
        add: Add the label when true; remove it otherwise.
        comment: Optional comment body to add with the label update.
        session: Optional HTTP session passed to ``jira_api_put``.
    """
    operation = "add" if add else "remove"
    update: dict[str, Any] = {"labels": [{operation: label}]}
    if comment is not None:
        update["comment"] = [{"add": {"body": comment}}]
    await jira_api_put(
        f"issue/{quote(issue_key)}",
        json={"update": update},
        api_version="2",
        session=session,
    )


async def add_issue_label(issue_key: str, label: str, comment: str | None = None) -> None:
    """Add a Jira issue label and optional comment."""
    await update_issue_label(issue_key, label, add=True, comment=comment)


async def remove_issue_label(issue_key: str, label: str, comment: str | None = None) -> None:
    """Remove a Jira issue label and optionally add a comment."""
    await update_issue_label(issue_key, label, add=False, comment=comment)


def clear_custom_fields_cache() -> None:
    """Clear the per-Jira-instance custom field cache (primarily for tests)."""
    _custom_fields_by_url.clear()
