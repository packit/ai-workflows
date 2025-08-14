import datetime
import os
import json
from enum import Enum
from typing import Annotated, Any
from urllib.parse import urljoin

import requests
from pydantic import Field


# Jira custom field IDs
SEVERITY_CUSTOM_FIELD = "customfield_12316142"
TARGET_END_CUSTOM_FIELD = "customfield_12313942"


class Severity(Enum):
    NONE = "None"
    INFORMATIONAL = "Informational"
    LOW = "Low"
    MODERATE = "Moderate"
    IMPORTANT = "Important"
    CRITICAL = "Critical"


class PreliminaryTesting(Enum):
    NONE = "None"
    PASS = "Pass"
    FAIL = "Fail"
    REQUESTED = "Requested"


def _get_jira_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_jira_details(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> dict[str, Any] | str:
    """
    Gets details about the specified Jira issue, including all comments and remote links.
    Returns a dictionary with issue details and comments or an error message on failure.
    """
    headers = _get_jira_headers(os.getenv("JIRA_TOKEN"))

    # Get main issue data
    try:
        response = requests.get(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
            params={"expand": "comments"},
            headers=headers,
        )
        response.raise_for_status()
        issue_data = response.json()
    except requests.RequestException as e:
        return f"Failed to get details about the specified issue: {e}"

    # get remote links - these often contain links to PRs or mailing lists
    try:
        remote_links_response = requests.get(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/remotelink"),
            headers=headers,
        )
        remote_links_response.raise_for_status()
        remote_links = remote_links_response.json()
        issue_data["remote_links"] = remote_links
    except requests.RequestException as e:
        # If remote links fail, continue without them
        issue_data["remote_links"] = []

    return issue_data



def set_jira_fields(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    fix_versions: Annotated[
        list[str] | None,
        Field(description="List of Fix Version/s values (e.g., ['rhel-9.8'], ['rhel-9.7.z'])"),
    ] = None,
    severity: Annotated[Severity | None, Field(description="Severity value")] = None,
    target_end: Annotated[datetime.date | None, Field(description="Target End value")] = None,
) -> str:
    """
    Updates the specified Jira issue, setting only the fields that are currently empty/unset.
    """
    if os.getenv("DRY_RUN", "False").lower() == "true":
        return "Dry run, not updating Jira fields"

    # First, get the current issue to check existing field values
    try:
        response = requests.get(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
            headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
        )
        response.raise_for_status()
        current_issue = response.json()
    except requests.RequestException as e:
        return f"Failed to get current issue details: {e}"

    fields = {}
    current_fields = current_issue.get("fields", {})

    if fix_versions is not None:
        current_fix_versions = current_fields.get("fixVersions", [])
        if not current_fix_versions:
            fields["fixVersions"] = [{"name": fv} for fv in fix_versions]

    if severity is not None:
        current_severity = current_fields.get(SEVERITY_CUSTOM_FIELD)
        if not current_severity.get("value"):
            fields[SEVERITY_CUSTOM_FIELD] = {"value": severity.value}

    if target_end is not None:
        current_target_end = current_fields.get(TARGET_END_CUSTOM_FIELD)
        if not current_target_end.get("value"):
            fields[TARGET_END_CUSTOM_FIELD] = target_end.strftime("%Y-%m-%d")

    if not fields:
        return f"No fields needed updating in {issue_key}"

    try:
        response = requests.put(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
            json={"fields": fields},
            headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
        )
        response.raise_for_status()
    except requests.RequestException as e:
        return f"Failed to set the specified fields: {e}"

    return f"Successfully updated {issue_key}"


def add_jira_comment(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    comment: Annotated[str, Field(description="Comment text to add")],
) -> str:
    """
    Adds a comment to the specified Jira issue.
    """
    try:
        response = requests.post(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/comment"),
            json={"body": comment},
            headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
        )
        response.raise_for_status()
    except requests.RequestException as e:
        return f"Failed to add the specified comment: {e}"
    return f"Successfully added the specified comment to {issue_key}"

def add_private_jira_comment(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    comment: Annotated[str, Field(description="Comment text to add")],
) -> str:
    """
    Adds a private comment to the specified Jira issue.
    """

    if os.getenv("DRY_RUN", "False").lower() == "true":
        return "Dry run, not adding private comment"

    try:
        response = requests.post(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/comment"),
            json={"body": comment, "visibility": {"type":"group", "value":"Red Hat Employee"}},
            headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
        )
        response.raise_for_status()
    except requests.RequestException as e:
        return f"Failed to add the specified comment: {e}"
    return f"Successfully added the specified comment to {issue_key}"
