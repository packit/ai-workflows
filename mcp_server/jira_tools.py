import datetime
import logging
import os
import json
import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)

if os.getenv("MOCK_JIRA", "False").lower() == "true":
    from aiohttp_client_session_mock import aiohttpClientSessionMock as aiohttpClientSession
else:
    from aiohttp import ClientSession as aiohttpClientSession

from fastmcp.exceptions import ToolError
from pydantic import Field

from common import CVEEligibilityResult, load_rhel_config
from common.constants import JIRA_SEARCH_PATH
from common.utils import get_jira_auth_headers

def _skip_jira_writes() -> bool:
    return os.getenv("SKIP_JIRA", "False").lower() == "true"


# Jira custom field IDs
SEVERITY_CUSTOM_FIELD = "customfield_12316142"
TARGET_END_CUSTOM_FIELD = "customfield_12313942"
EMBARGO_CUSTOM_FIELD = "customfield_12324750"


RH_EMPLOYEE_GROUP = "Red Hat Employee"

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




async def get_jira_details(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> dict[str, Any]:
    """
    Gets details about the specified Jira issue, including all comments and remote links.
    Returns a dictionary with issue details and comments.
    """
    headers = get_jira_auth_headers()
    jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}")
    logger.info(f"Connecting to JIRA API to get issue details: {jira_url}")

    async with aiohttpClientSession() as session:
        # Get main issue data
        try:
            async with session.get(
                jira_url,
                params={"expand": "comments"},
                headers=headers,
            ) as response:
                response.raise_for_status()
                issue_data = await response.json()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to get details about the specified issue: {e}") from e

        # get remote links - these often contain links to PRs or mailing lists
        try:
            async with session.get(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}/remotelink"),
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
    if os.getenv("SKIP_SETTING_JIRA_FIELDS", "False").lower() == "true":
        return "Skipping of setting Jira fields requested, not doing anything (this is expected, not an error)"
    if os.getenv("DRY_RUN", "False").lower() == "true":
        return "Dry run, not updating Jira fields (this is expected, not an error)"
    if _skip_jira_writes():
        return f"SKIP_JIRA is set, not updating fields on {issue_key} (this is expected, not an error)"

    async with aiohttpClientSession() as session:
        # First, get the current issue to check existing field values
        jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}")
        logger.info(f"Connecting to JIRA API to set fields for issue: {jira_url}")
        try:
            async with session.get(
                jira_url,
                headers=get_jira_auth_headers(),
            ) as response:
                response.raise_for_status()
                current_issue = await response.json()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to get current issue details: {e}") from e

        fields = {}
        current_fields = current_issue.get("fields", {})

        if fix_versions is not None:
            current_fix_versions = current_fields.get("fixVersions", [])
            if not current_fix_versions:
                fields["fixVersions"] = [{"name": fv} for fv in fix_versions]

        if severity is not None:
            current_severity = current_fields.get(SEVERITY_CUSTOM_FIELD)
            if current_severity is None or not current_severity.get("value"):
                fields[SEVERITY_CUSTOM_FIELD] = {"value": severity.value}

        if target_end is not None:
            current_target_end = current_fields.get(TARGET_END_CUSTOM_FIELD)
            if current_target_end is None or not current_target_end.get("value"):
                fields[TARGET_END_CUSTOM_FIELD] = target_end.strftime("%Y-%m-%d")

        if not fields:
            return f"No fields needed updating in {issue_key}"

    async with aiohttpClientSession() as session:
        try:
            async with session.put(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}"),
                json={"fields": fields},
                headers=get_jira_auth_headers(),
            ) as response:
                response.raise_for_status()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to set the specified fields: {e}") from e

    return f"Successfully updated {issue_key}"


async def add_jira_comment(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    comment: Annotated[str, Field(description="Comment text to add")],
    private: Annotated[bool, Field(description="Whether the comment should be hidden from public")] = False,
) -> str:
    """
    Adds a comment to the specified Jira issue.
    """
    if os.getenv("DRY_RUN", "False").lower() == "true":
        return f"Dry run, not adding comment to {issue_key} (this is expected, not an error)"
    if _skip_jira_writes():
        return f"SKIP_JIRA is set, not adding comment to {issue_key} (this is expected, not an error)"

    # Jira REST API v3 does not support markdown or wiki markup in comments.
    jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/comment")
    logger.info(f"Connecting to JIRA API to add comment: {jira_url}")
    async with aiohttpClientSession() as session:
        try:
            async with session.post(
                jira_url,
                json={
                    "body": comment,
                    **({"visibility": {"type": "group", "value": RH_EMPLOYEE_GROUP}} if private else {}),
                },
                headers=get_jira_auth_headers(),
            ) as response:
                response.raise_for_status()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to add the specified comment: {e}") from e
    return f"Successfully added the specified comment to {issue_key}"


async def check_cve_triage_eligibility(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> CVEEligibilityResult:
    """
    Analyzes if a Jira issue represents a CVE and determines if it should be processed by triage agent.
    Only process CVEs if they are Z-stream (based on fixVersion).

    Returns CVEEligibilityResult model with eligibility decision and reasoning.
    """
    headers = get_jira_auth_headers()
    jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}")
    logger.info(f"Connecting to JIRA API to check CVE eligibility: {jira_url}")

    async with aiohttpClientSession() as session:
        try:
            async with session.get(
                jira_url,
                headers=headers,
            ) as response:
                response.raise_for_status()
                jira_data = await response.json()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to get Jira data: {e}") from e

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
            reason="CVE has no target release specified",
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

    rhel_config = await load_rhel_config()
    upcoming_z_streams = rhel_config.get("upcoming_z_streams", {})
    current_z_streams = rhel_config.get("current_z_streams", {})
    latest_z_streams = current_z_streams | upcoming_z_streams

    needs_internal_fix = False
    severity = fields.get(SEVERITY_CUSTOM_FIELD, {}).get("value", "")

    # Check if z-stream is not in latest z-streams - always needs internal fix
    if target_version.lower() not in [v.lower() for v in latest_z_streams.values()]:
        needs_internal_fix = True
        reason = f"Z-stream CVE ({target_version}) not in latest z-streams, needs RHEL fix first"
    # Determine if internal fix is needed based on severity
    elif severity not in [Severity.LOW.value, Severity.MODERATE.value]:
        needs_internal_fix = True
        reason = f"High severity CVE ({severity}) eligible for Z-stream, needs RHEL fix first"
    else:
        reason = "CVE eligible for Z-stream fix in CentOS Stream"

    return CVEEligibilityResult(
        is_cve=True,
        is_eligible_for_triage=True,
        reason=reason,
        needs_internal_fix=needs_internal_fix
    )


async def change_jira_status(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    status: Annotated[str, Field(description="New status to transition to (e.g. 'In Progress', 'Done', 'Closed')")],
) -> str:
    if os.getenv("DRY_RUN", "False").lower() == "true":
        return f"Dry run, not changing status of {issue_key} to {status}  (this is expected, not an error)"
    if _skip_jira_writes():
        return f"SKIP_JIRA is set, not changing status of {issue_key} (this is expected, not an error)"

    headers = get_jira_auth_headers()
    jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}/transitions")
    logger.info(f"Connecting to JIRA API to change status: {jira_url}")

    async with aiohttpClientSession() as session:
        try:
            async with session.get(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}"),
                params={"fields": "status"},
                headers=headers,
            ) as response:
                response.raise_for_status()
                current_issue = await response.json()
                current_status = current_issue.get("fields", {}).get("status", {}).get("name", "")
                if current_status.lower() == status.lower():
                    return f"JIRA issue {issue_key} is already in status '{current_status}', no change needed (this is expected, not an error)"
        except aiohttp.ClientError as e:
            # if we can't check current status, continue with transition attempt
            pass

        # get available transitions
        try:
            async with session.get(jira_url, headers=headers) as resp:
                resp.raise_for_status()
                transitions = (await resp.json()).get("transitions", [])
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to get available transitions for {issue_key}: {e}") from e

        # get desired status
        transition = next(
            (t for t in transitions if t.get("to", {}).get("name", "").lower() == status.lower()),
            None,
        )

        if not transition:
            available = ", ".join(t.get("to", {}).get("name", "?") for t in transitions)
            raise ToolError(f"Status '{status}' is not available for {issue_key}. Available: {available}")

        # do the transition
        try:
            async with session.post(jira_url, json={"transition": {"id": transition["id"]}}, headers=headers) as resp:
                resp.raise_for_status()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to change status of {issue_key} to {status}: {e}") from e

    return f"Successfully changed status of {issue_key} to {status}"



async def edit_jira_labels(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    labels_to_add: Annotated[list[str] | None, Field(description="List of labels to add to the issue")] = None,
    labels_to_remove: Annotated[list[str] | None, Field(description="List of labels to remove from the issue")] = None,
) -> str:
    """
    Edits labels on a Jira issue by adding and/or removing specified labels.
    """
    if not (labels_to_add or labels_to_remove):
        return f"No label changes requested for {issue_key}"

    if os.getenv("DRY_RUN", "False").lower() == "true":
        return f"Dry run, not editing labels on {issue_key} (this is expected, not an error)"
    if _skip_jira_writes():
        return f"SKIP_JIRA is set, not editing labels on {issue_key} (this is expected, not an error)"

    jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}")
    logger.info(f"Connecting to JIRA API to edit labels: {jira_url}")
    headers = get_jira_auth_headers()

    update_payload = []
    if labels_to_add:
        update_payload.extend([{"add": label} for label in labels_to_add])
    if labels_to_remove:
        update_payload.extend([{"remove": label} for label in labels_to_remove])

    payload = {"update": {"labels": update_payload}}

    async with aiohttpClientSession() as session:
        try:
            async with session.put(
                jira_url,
                json=payload,
                headers=headers,
            ) as response:
                response.raise_for_status()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to edit labels on {issue_key}: {e}") from e

    return f"Successfully edited labels on {issue_key}."


async def verify_issue_author(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> bool:
    """
    Verifies if the author of the Jira issue is a Red Hat employee by checking their group membership.
    Supports both Jira Server (using 'key') and Jira Cloud (using 'accountId').
    """
    headers = get_jira_auth_headers()
    jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}")
    logger.info(f"Connecting to JIRA API to verify issue author: {jira_url}")

    async with aiohttpClientSession() as session:
        try:
            async with session.get(
                jira_url,
                headers=headers,
            ) as response:
                response.raise_for_status()
                issue_data = await response.json()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to get Jira data: {e}") from e

        reporter = issue_data.get("fields", {}).get("reporter", {})

        # Try both Jira Server (key) and Jira Cloud (accountId)
        author_key = reporter.get("key")
        author_account_id = reporter.get("accountId")

        if not author_key and not author_account_id:
            return False

        # Build params based on what's available
        params = {"expand": "groups"}
        if author_account_id:
            params["accountId"] = author_account_id
        elif author_key:
            params["key"] = author_key

        try:
            async with session.get(
                urljoin(os.getenv("JIRA_URL"), f"rest/api/3/user"),
                params=params,
                headers=headers,
            ) as user_response:
                user_response.raise_for_status()
                user_data = await user_response.json()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to get user groups: {e}") from e

        return any(
            group.get("name") == RH_EMPLOYEE_GROUP
            for group in user_data.get("groups", {}).get("items", [])
        )


async def search_jira_issues(
    jql: Annotated[str, Field(description="JQL query string (e.g., 'component = \"fence-agents\" AND summary ~ \"fix missing statuses\"')")],
    fields: Annotated[
        list[str] | None,
        Field(description="List of fields to return (e.g., ['key', 'id', 'summary', 'fixVersions']). Defaults to key, id, summary, fixVersions."),
    ] = None,
    max_results: Annotated[int, Field(description="Maximum number of results to return")] = 50,
) -> list[dict[str, Any]]:
    """
    Searches Jira using the provided JQL query and returns matching issues
    with the specified fields.
    """
    if fields is None:
        fields = ["key", "summary", "fixVersions"]

    headers = get_jira_auth_headers()
    url = urljoin(os.getenv("JIRA_URL"), JIRA_SEARCH_PATH)
    logger.info(f"Searching Jira with JQL: {jql}")

    json_payload = {
        "jql": jql,
        "maxResults": max_results,
        "fields": fields,
    }

    async with aiohttpClientSession() as session:
        try:
            async with session.post(
                url,
                json=json_payload,
                headers=headers,
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to search Jira issues: {e}") from e

    issues = data.get("issues", [])
    logger.info(f"Jira search returned {len(issues)} issues")

    return [
        {
            "key": issue.get("key"),
            "id": issue.get("id"),
            "fields": {f: issue.get("fields", {}).get(f) for f in fields if f not in ("key", "id")},
        }
        for issue in issues
    ]


async def get_jira_dev_status(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> list[dict[str, Any]]:
    """
    Gets development status (linked commits) for a Jira issue using the
    Jira Dev-Status API. Returns a list of commit objects with
    url, message, and repository_url fields.
    """
    headers = get_jira_auth_headers()
    jira_base = os.getenv("JIRA_URL")

    logger.info(f"Fetching development status for {issue_key}")

    async with aiohttpClientSession() as session:
        # Resolve the issue key to its numeric ID
        # (the dev-status API requires issueId, not issueKey)
        issue_url = urljoin(jira_base, f"rest/api/3/issue/{issue_key}")
        try:
            async with session.get(
                issue_url,
                params={"fields": ""},
                headers=headers,
            ) as response:
                response.raise_for_status()
                issue_data = await response.json()
                issue_id = issue_data["id"]
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to resolve issue ID for {issue_key}: {e}") from e

        # Get the dev-status summary first to discover valid applicationType values
        summary_url = urljoin(
            jira_base,
            f"rest/dev-status/1.0/issue/summary?issueId={issue_id}",
        )
        try:
            async with session.get(
                summary_url,
                headers=headers,
            ) as response:
                response.raise_for_status()
                summary_data = await response.json()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to get dev status summary for {issue_key}: {e}") from e

        # Collect commits from each application type reported in the summary
        commits = []
        for provider in summary_data.get("summary", {}).get("repository", {}).get("byInstanceType", {}).values():
            app_type = provider.get("applicationType")
            if not app_type:
                continue

            dev_status_url = urljoin(
                jira_base,
                f"rest/dev-status/1.0/issue/detail?issueId={issue_id}&applicationType={app_type}&dataType=repository",
            )
            try:
                async with session.get(
                    dev_status_url,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    dev_data = await response.json()
            except aiohttp.ClientError as e:
                logger.warning(f"Failed to get dev status detail for {issue_key} (applicationType={app_type}): {e}")
                continue

            for detail in dev_data.get("detail", []):
                for repo in detail.get("repositories", []):
                    repo_url = repo.get("url", "")
                    for commit in repo.get("commits", []):
                        commits.append({
                            "url": commit.get("url", ""),
                            "message": commit.get("message", ""),
                            "repository_url": repo_url,
                        })

    logger.info(f"Found {len(commits)} commits in development status for {issue_key}")
    return commits
