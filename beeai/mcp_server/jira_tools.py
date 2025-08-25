import datetime
import os
import json
import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urljoin

import aiohttp
from pydantic import Field

from common import CVEEligibilityResult

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


async def get_jira_details(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> dict[str, Any] | str:
    """
    Gets details about the specified Jira issue, including all comments and remote links.
    Returns a dictionary with issue details and comments or an error message on failure.
    """
    headers = _get_jira_headers(os.getenv("JIRA_TOKEN"))

    async with aiohttp.ClientSession() as session:
        # Get main issue data
        try:
            async with session.get(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
                params={"expand": "comments"},
                headers=headers,
            ) as response:
                response.raise_for_status()
                issue_data = await response.json()
        except aiohttp.ClientError as e:
            return f"Failed to get details about the specified issue: {e}"

        # get remote links - these often contain links to PRs or mailing lists
        try:
            async with session.get(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/remotelink"),
                headers=headers,
            ) as remote_links_response:
                remote_links_response.raise_for_status()
                remote_links = await remote_links_response.json()
                issue_data["remote_links"] = remote_links
        except aiohttp.ClientError as e:
            # If remote links fail, continue without them
            issue_data["remote_links"] = []

    return issue_data


async def set_jira_fields(
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

    async with aiohttp.ClientSession() as session:
        # First, get the current issue to check existing field values
        try:
            async with session.get(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
                headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
            ) as response:
                response.raise_for_status()
                current_issue = await response.json()
        except aiohttp.ClientError as e:
            return f"Failed to get current issue details: {e}"

        fields = {}
        current_fields = current_issue.get("fields", {})

        if fix_versions is not None:
            current_fix_versions = current_fields.get("fixVersions", [])
            if not current_fix_versions:
                fields["fixVersions"] = [{"name": fv} for fv in fix_versions]

        if severity is not None:
            current_severity = current_fields.get(SEVERITY_CUSTOM_FIELD, {})
            if not current_severity.get("value"):
                fields[SEVERITY_CUSTOM_FIELD] = {"value": severity.value}

        if target_end is not None:
            current_target_end = current_fields.get(TARGET_END_CUSTOM_FIELD, {})
            if not current_target_end.get("value"):
                fields[TARGET_END_CUSTOM_FIELD] = target_end.strftime("%Y-%m-%d")

        if not fields:
            return f"No fields needed updating in {issue_key}"

        try:
            async with session.put(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
                json={"fields": fields},
                headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
            ) as response:
                response.raise_for_status()
        except aiohttp.ClientError as e:
            return f"Failed to set the specified fields: {e}"

    return f"Successfully updated {issue_key}"


async def add_jira_comment(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    comment: Annotated[str, Field(description="Comment text to add")],
    private: Annotated[bool, Field(description="Whether the comment should be hidden from public")] = False,
) -> str:
    """
    Adds a comment to the specified Jira issue.
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/comment"),
                json={
                    "body": comment,
                    **({"visibility": {"type": "group", "value": "Red Hat Employee"}} if private else {}),
                },
                headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
            ) as response:
                response.raise_for_status()
        except aiohttp.ClientError as e:
            return f"Failed to add the specified comment: {e}"
    return f"Successfully added the specified comment to {issue_key}"


async def check_cve_triage_eligibility(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> CVEEligibilityResult:
    """
    Analyzes if a Jira issue represents a CVE and determines if it should be processed by triage agent.
    Only process CVEs if they are Z-stream (based on fixVersion).

    Returns CVEEligibilityResult model with eligibility decision and reasoning.
    """
    headers = _get_jira_headers(os.getenv("JIRA_TOKEN"))

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
                headers=headers,
            ) as response:
                response.raise_for_status()
                jira_data = await response.json()
        except aiohttp.ClientError as e:
            return CVEEligibilityResult(
                is_cve=False,
                is_eligible_for_triage=False,
                reason="Failed to fetch Jira data",
                error=f"Failed to get Jira data: {e}"
            )

    fields = jira_data.get("fields", {})
    labels = fields.get("labels", [])

    # Non-CVEs are always eligible
    if "SecurityTracking" not in labels:
        return CVEEligibilityResult(
            is_cve=False,
            is_eligible_for_triage=True,
            reason="Not a CVE"
        )

    fix_versions = fields.get("fixVersions", [])
    if not fix_versions:
        return CVEEligibilityResult(
            is_cve=True,
            is_eligible_for_triage=False,
            error="CVE has no target release specified"
        )

    target_version = fix_versions[0].get("name", "")

    # Only process Z-stream CVEs (reject Y-stream)
    if re.match(r"^rhel-\d+\.\d+$", target_version.lower()):
        return CVEEligibilityResult(
            is_cve=True,
            is_eligible_for_triage=False,
            reason="Y-stream CVEs will be handled in Z-stream"
        )

    embargo = fields.get(EMBARGO_CUSTOM_FIELD, {}).get("value", "")
    if embargo == "True":
        return CVEEligibilityResult(
            is_cve=True,
            is_eligible_for_triage=False,
            reason="CVE is embargoed"
        )

    # Determine if internal fix is needed based on severity and priority
    severity = fields.get(SEVERITY_CUSTOM_FIELD, {}).get("value", "")
    priority_labels = [label for label in labels if label in PRIORITY_LABELS]

    needs_internal_fix = (
        severity not in [Severity.LOW.value, Severity.MODERATE.value] or
        bool(priority_labels)
    )

    if needs_internal_fix:
        if severity not in [Severity.LOW.value, Severity.MODERATE.value]:
            reason = f"High severity CVE ({severity}) eligible for Z-stream, needs RHEL fix first"
        else:
            reason = f"Priority CVE with labels {priority_labels} eligible for Z-stream, needs RHEL fix first"
    else:
        reason = "CVE eligible for Z-stream fix in CentOS Stream"

    return CVEEligibilityResult(
        is_cve=True,
        is_eligible_for_triage=True,
        reason=reason,
        needs_internal_fix=needs_internal_fix
    )
