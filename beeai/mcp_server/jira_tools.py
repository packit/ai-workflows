import datetime
import os
import json
import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urljoin

import requests
from pydantic import Field

# Jira custom field IDs
SEVERITY_CUSTOM_FIELD = "customfield_12316142"
TARGET_END_CUSTOM_FIELD = "customfield_12313942"
EMBARGO_CUSTOM_FIELD = "customfield_12324750"

PRIORITY_LABELS = ["compliance-priority", "contract-priority"]

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

def check_cve_triage_eligibility(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> dict[str, Any]:
    """
    Analyzes if a Jira issue represents a CVE and determines if it should be processed by triage agent.
    Implements logic based on Y-stream vs Z-stream releases and particular conditions.

    Returns dictionary with:
    - is_cve: bool - Whether this is a CVE (by type or label)
    - is_eligible_for_triage: bool - Whether triage agent should process it
    - reason: str - Explanation of the decision
    - branch: str - Target branch for MR (rhel-{version} or cNs)
    - error: str - Error message if the issue cannot be processed
    """
    headers = _get_jira_headers(os.getenv("JIRA_TOKEN"))

    try:
        response = requests.get(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
            headers=headers,
        )
        response.raise_for_status()
        jira_data = response.json()
    except requests.RequestException as e:
        return {
            "is_eligible_for_triage": False,
            "error": f"Failed to get Jira data: {e}",
        }

    fields = jira_data.get("fields", {})
    labels = fields.get("labels", [])

    # Non-CVEs are always eligible
    if "SecurityTracking" not in labels:
        return {"is_cve": False, "is_eligible_for_triage": True, "reason": "Not a CVE"}

    # CVE processing - get target version
    fix_versions = fields.get("fixVersions", [])
    if not fix_versions:
        return {
            "is_cve": True,
            "is_eligible_for_triage": False,
            "error": "CVE has no target release specified",
        }

    target_version = fix_versions[0].get("name", "")
    is_y_stream = bool(re.match(r"^rhel-\d+\.\d+$", target_version.lower()))

    version_match = re.match(r"^rhel-(\d+)", target_version.lower())
    if not version_match:
        return {"is_cve": True, "is_eligible_for_triage": False, "error": "Not possible to determine major version"}

    major_version = version_match.group(1)
    rhel_branch = target_version # TODO check how to map to branch
    cns_branch = f"c{major_version}s"

    embargo = fields.get(EMBARGO_CUSTOM_FIELD, {}).get("value", "")
    if embargo == "True":
        return {
            "is_cve": True,
            "is_eligible_for_triage": False,
            "reason": "CVE is embargoed",
        }

    severity = fields.get(SEVERITY_CUSTOM_FIELD, {}).get("value", "")
    priority_labels = [l for l in labels if l in PRIORITY_LABELS]

    if severity not in [Severity.LOW.value, Severity.MODERATE.value]:
        if is_y_stream:
            return {
                "is_cve": True,
                "is_eligible_for_triage": False,
                "reason": f"CVE severity is {severity}, not Low/Moderate, and target version is Y-stream",
            }
        return {
            "is_cve": True,
            "is_eligible_for_triage": True,
            "reason": f"CVE severity is {severity}, eligible for Z-stream, needs to be fixed in RHEL",
            "branch": rhel_branch,
        }

    if priority_labels:
        if is_y_stream:
            return {
                "is_cve": True,
                "is_eligible_for_triage": False,
                "reason": f"CVE has priority labels: {', '.join(priority_labels)}, and target version is Y-stream",
            }
        return {
            "is_cve": True,
            "is_eligible_for_triage": True,
            "reason": f"CVE has priority labels: {', '.join(priority_labels)}, eligible for Z-stream, needs to be fixed in RHEL.",
            "branch": rhel_branch,
        }

    if major_version == "8":
        #  no Y-stream, let's skip the other check
        return {
        "is_cve": True,
        "is_eligible_for_triage": True,
        "reason": "Z-stream CVE, no Y-stream",
        "needs_internal_fix": False,
        }

    due_date = fields.get("duedate")

    release_dates = _load_release_dates()
    config = _load_rhel_config()

    current_y_streams = config.get("current_y_streams")

    y_stream_version = current_y_streams.get(major_version)
    y_stream_release_date = release_dates.get(y_stream_version.lower()) if y_stream_version else None

    # Compare due date with Y-stream release date to determine Y vs Z stream processing
    is_due_after_y_release = _is_due_after_release(due_date, y_stream_release_date)

    if is_y_stream:
        return {
            "is_cve": True,
            "is_eligible_for_triage": is_due_after_y_release,
            "reason": f"Y-stream CVE: {'eligible' if is_due_after_y_release else 'not eligible'} (due {'after' if is_due_after_y_release else 'before'} release)",
            **({"branch": cns_branch} if is_due_after_y_release else {}),
        }

    # Z-stream: eligible only if due before Y-stream release
    return {
        "is_cve": True,
        "is_eligible_for_triage": not is_due_after_y_release,
        "reason": f"Z-stream CVE: {'not eligible - handled by Y-stream' if is_due_after_y_release else 'eligible for Z-stream fix'}",
        **({"branch": cns_branch} if not is_due_after_y_release else {}),
    }


def _load_rhel_config() -> dict[str, Any]:
    """
    Load RHEL configuration from rhel-config.json file.

    Returns:
        Dictionary containing RHEL configuration, empty dict if file not found
    """
    config_file = "rhel-config.json"

    if not Path(config_file).exists():
        raise Exception(f"RHEL config file {config_file} not found")

    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def _is_due_after_release(due_date: str | None, release_date: str | None) -> bool:
    """
    Compare due date with release date to determine if CVE is due after release.

    Args:
        due_date: CVE due date from Jira (YYYY-MM-DD format)
        release_date: RHEL version release date (YYYY-MM-DD format)

    Returns:
        bool: True if due date is after release date, False otherwise.
              Returns False if either date is None (conservative approach).
    """
    # Let's consider that if due date is not set, it's ok to handle this in Y-stream
    if not due_date:
        return True
    if not release_date:
        return False

    try:
        due_dt = datetime.datetime.strptime(due_date, "%Y-%m-%d")
        release_dt = datetime.datetime.strptime(release_date, "%Y-%m-%d")
        return due_dt > release_dt
    except ValueError as e:
        return False
