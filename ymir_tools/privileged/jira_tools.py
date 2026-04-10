import datetime
import logging
import os
import re
from enum import Enum
from typing import Any
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)

if os.getenv("MOCK_JIRA", "False").lower() == "true":
    from aiohttp_client_session_mock import aiohttpClientSessionMock as aiohttpClientSession
else:
    from aiohttp import ClientSession as aiohttpClientSession

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, StringToolOutput, Tool, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir_common import CVEEligibilityResult, load_rhel_config
from ymir_common.constants import JIRA_SEARCH_PATH
from ymir_common.utils import get_jira_auth_headers

def _skip_jira_writes() -> bool:
    return os.getenv("JIRA_DRY_RUN", "False").lower() == "true"


# Jira custom field IDs
SEVERITY_CUSTOM_FIELD = "customfield_10840"
TARGET_END_CUSTOM_FIELD = "customfield_10023"
EMBARGO_CUSTOM_FIELD = "customfield_10860"

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


class GetJiraDetailsToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")


class GetJiraDetailsTool(Tool[GetJiraDetailsToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]):
    name = "get_jira_details"
    description = """
    Gets details about the specified Jira issue, including all comments and remote links.
    Returns a dictionary with issue details and comments.
    """
    input_schema = GetJiraDetailsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetJiraDetailsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        issue_key = tool_input.issue_key
        headers = get_jira_auth_headers()
        jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}")
        logger.info(f"Connecting to JIRA API to get issue details: {jira_url}")

        async with aiohttpClientSession() as session:
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

            try:
                async with session.get(
                    urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}/remotelink"),
                    headers=headers,
                ) as remote_links_response:
                    remote_links_response.raise_for_status()
                    remote_links = await remote_links_response.json()
                    issue_data["remote_links"] = remote_links
            except aiohttp.ClientError:
                issue_data["remote_links"] = []

        return JSONToolOutput(result=issue_data)


class SetJiraFieldsToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")
    fix_versions: list[str] | None = Field(
        default=None,
        description="List of Fix Version/s values (e.g., ['rhel-9.8'], ['rhel-9.7.z'])",
    )
    severity: Severity | None = Field(default=None, description="Severity value")
    target_end: datetime.date | None = Field(default=None, description="Target End value")


class SetJiraFieldsTool(Tool[SetJiraFieldsToolInput, ToolRunOptions, StringToolOutput]):
    name = "set_jira_fields"
    description = """
    Updates the specified Jira issue, setting only the fields that are currently empty/unset.
    """
    input_schema = SetJiraFieldsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: SetJiraFieldsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        issue_key = tool_input.issue_key
        fix_versions = tool_input.fix_versions
        severity = tool_input.severity
        target_end = tool_input.target_end
        if os.getenv("SKIP_SETTING_JIRA_FIELDS", "False").lower() == "true":
            return StringToolOutput(
                result="Skipping of setting Jira fields requested, not doing anything (this is expected, not an error)"
            )
        if os.getenv("DRY_RUN", "False").lower() == "true":
            return StringToolOutput(result="Dry run, not updating Jira fields (this is expected, not an error)")

        async with aiohttpClientSession() as session:
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
                return StringToolOutput(result=f"No fields needed updating in {issue_key}")

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

        return StringToolOutput(result=f"Successfully updated {issue_key}")


class AddJiraCommentToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")
    comment: str = Field(description="Comment text to add")
    private: bool = Field(default=False, description="Whether the comment should be hidden from public")


class AddJiraCommentTool(Tool[AddJiraCommentToolInput, ToolRunOptions, StringToolOutput]):
    name = "add_jira_comment"
    description = """
    Adds a comment to the specified Jira issue.
    """
    input_schema = AddJiraCommentToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: AddJiraCommentToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        issue_key = tool_input.issue_key
        comment = tool_input.comment
        private = tool_input.private
        if os.getenv("DRY_RUN", "False").lower() == "true":
            return StringToolOutput(
                result=f"Dry run, not adding comment to {issue_key} (this is expected, not an error)"
            )

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
        return StringToolOutput(result=f"Successfully added the specified comment to {issue_key}")


class CheckCveTriageEligibilityToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")

class CheckCveTriageEligibilityTool(
    Tool[CheckCveTriageEligibilityToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "check_cve_triage_eligibility"
    description = """
    Analyzes if a Jira issue represents a CVE and determines if it should be processed by triage agent.
    Only process CVEs if they are Z-stream (based on fixVersion).

    Returns CVEEligibilityResult model with eligibility decision and reasoning.
    """
    input_schema = CheckCveTriageEligibilityToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: CheckCveTriageEligibilityToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        issue_key = tool_input.issue_key
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

        if "SecurityTracking" not in labels:
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=False,
                    is_eligible_for_triage=True,
                    reason="Not a CVE",
                ).model_dump()
            )

        fix_versions = fields.get("fixVersions", [])
        if not fix_versions:
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    is_eligible_for_triage=False,
                    reason="CVE has no target release specified",
                    error="CVE has no target release specified",
                ).model_dump()
            )

        target_version = fix_versions[0].get("name", "")

        if re.match(r"^rhel-\d+\.\d+$", target_version.lower()):
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    is_eligible_for_triage=False,
                    reason="Y-stream CVEs will be handled in Z-stream",
                ).model_dump()
            )

        embargo = fields.get(EMBARGO_CUSTOM_FIELD, {}).get("value", "")
        if embargo == "True":
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    is_eligible_for_triage=False,
                    reason="CVE is embargoed",
                ).model_dump()
            )

        rhel_config = await load_rhel_config()
        upcoming_z_streams = rhel_config.get("upcoming_z_streams", {})
        current_z_streams = rhel_config.get("current_z_streams", {})
        latest_z_streams = current_z_streams | upcoming_z_streams

        needs_internal_fix = False
        severity = fields.get(SEVERITY_CUSTOM_FIELD, {}).get("value", "")

        if target_version.lower() not in [v.lower() for v in latest_z_streams.values()]:
            needs_internal_fix = True
            reason = f"Z-stream CVE ({target_version}) not in latest z-streams, needs RHEL fix first"
        elif severity not in [Severity.LOW.value, Severity.MODERATE.value]:
            needs_internal_fix = True
            reason = f"High severity CVE ({severity}) eligible for Z-stream, needs RHEL fix first"
        else:
            reason = "CVE eligible for Z-stream fix in CentOS Stream"

        return JSONToolOutput(
            CVEEligibilityResult(
                is_cve=True,
                is_eligible_for_triage=True,
                reason=reason,
                needs_internal_fix=needs_internal_fix,
            ).model_dump()
        )


class ChangeJiraStatusToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")
    status: str = Field(
        description="New status to transition to (e.g. 'In Progress', 'Done', 'Closed')",
    )


class ChangeJiraStatusTool(Tool[ChangeJiraStatusToolInput, ToolRunOptions, StringToolOutput]):
    name = "change_jira_status"
    description = """
    Transitions a Jira issue to the requested status when available.
    """
    input_schema = ChangeJiraStatusToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: ChangeJiraStatusToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        issue_key = tool_input.issue_key
        status = tool_input.status
        if os.getenv("DRY_RUN", "False").lower() == "true":
            return StringToolOutput(
                result=f"Dry run, not changing status of {issue_key} to {status}  (this is expected, not an error)"
            )

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
                        return StringToolOutput(
                            result=(
                                f"JIRA issue {issue_key} is already in status '{current_status}', "
                                "no change needed (this is expected, not an error)"
                            )
                        )
            except aiohttp.ClientError:
                pass

            try:
                async with session.get(jira_url, headers=headers) as resp:
                    resp.raise_for_status()
                    transitions = (await resp.json()).get("transitions", [])
            except aiohttp.ClientError as e:
                raise ToolError(f"Failed to get available transitions for {issue_key}: {e}") from e

            transition = next(
                (t for t in transitions if t.get("to", {}).get("name", "").lower() == status.lower()),
                None,
            )

            if not transition:
                available = ", ".join(t.get("to", {}).get("name", "?") for t in transitions)
                raise ToolError(f"Status '{status}' is not available for {issue_key}. Available: {available}")

            try:
                async with session.post(
                    jira_url, json={"transition": {"id": transition["id"]}}, headers=headers
                ) as resp:
                    resp.raise_for_status()
            except aiohttp.ClientError as e:
                raise ToolError(f"Failed to change status of {issue_key} to {status}: {e}") from e

        return StringToolOutput(result=f"Successfully changed status of {issue_key} to {status}")


class EditJiraLabelsToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")
    labels_to_add: list[str] | None = Field(default=None, description="List of labels to add to the issue")
    labels_to_remove: list[str] | None = Field(default=None, description="List of labels to remove from the issue")


class EditJiraLabelsTool(Tool[EditJiraLabelsToolInput, ToolRunOptions, StringToolOutput]):
    name = "edit_jira_labels"
    description = """
    Edits labels on a Jira issue by adding and/or removing specified labels.
    """
    input_schema = EditJiraLabelsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: EditJiraLabelsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        issue_key = tool_input.issue_key
        labels_to_add = tool_input.labels_to_add
        labels_to_remove = tool_input.labels_to_remove
        if not (labels_to_add or labels_to_remove):
            return StringToolOutput(result=f"No label changes requested for {issue_key}")

        if os.getenv("DRY_RUN", "False").lower() == "true":
            return StringToolOutput(
                result=f"Dry run, not editing labels on {issue_key} (this is expected, not an error)"
            )

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

        return StringToolOutput(result=f"Successfully edited labels on {issue_key}.")


class VerifyIssueAuthorToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")


class VerifyIssueAuthorTool(Tool[VerifyIssueAuthorToolInput, ToolRunOptions, JSONToolOutput[bool]]):
    name = "verify_issue_author"
    description = """
    Verifies if the author of the Jira issue is a Red Hat employee by checking their group membership.
    Supports both Jira Server (using 'key') and Jira Cloud (using 'accountId').
    """
    input_schema = VerifyIssueAuthorToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: VerifyIssueAuthorToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[bool]:
        issue_key = tool_input.issue_key
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

            author_key = reporter.get("key")
            author_account_id = reporter.get("accountId")

            if not author_key and not author_account_id:
                return JSONToolOutput(result=False)

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

            ok = any(
                group.get("name") == RH_EMPLOYEE_GROUP
                for group in user_data.get("groups", {}).get("items", [])
            )
            return JSONToolOutput(result=ok)


class SearchJiraIssuesToolInput(BaseModel):
    jql: str = Field(
        description='JQL query string (e.g., \'component = "fence-agents" AND summary ~ "fix missing statuses"\')',
    )
    fields: list[str] | None = Field(
        default=None,
        description=(
            "List of fields to return (e.g., ['key', 'id', 'summary', 'fixVersions']). "
            "Defaults to key, id, summary, fixVersions."
        ),
    )
    max_results: int = Field(default=50, description="Maximum number of results to return")


class SearchJiraIssuesTool(Tool[SearchJiraIssuesToolInput, ToolRunOptions, JSONToolOutput[list[dict[str, Any]]]]):
    name = "search_jira_issues"
    description = """
    Searches Jira using the provided JQL query and returns matching issues
    with the specified fields.
    """
    input_schema = SearchJiraIssuesToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: SearchJiraIssuesToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        jql = tool_input.jql
        fields = tool_input.fields if tool_input.fields is not None else ["key", "summary", "fixVersions"]
        max_results = tool_input.max_results

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

        out = [
            {
                "key": issue.get("key"),
                "id": issue.get("id"),
                "fields": {f: issue.get("fields", {}).get(f) for f in fields if f not in ("key", "id")},
            }
            for issue in issues
        ]
        return JSONToolOutput(result=out)


class GetJiraDevStatusToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")


class GetJiraDevStatusTool(Tool[GetJiraDevStatusToolInput, ToolRunOptions, JSONToolOutput[list[dict[str, Any]]]]):
    name = "get_jira_dev_status"
    description = """
    Gets development status (linked commits) for a Jira issue using the
    Jira Dev-Status API. Returns a list of commit objects with
    url, message, and repository_url fields.
    """
    input_schema = GetJiraDevStatusToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetJiraDevStatusToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        issue_key = tool_input.issue_key
        headers = get_jira_auth_headers()
        jira_base = os.getenv("JIRA_URL")

        logger.info(f"Fetching development status for {issue_key}")

        async with aiohttpClientSession() as session:
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
                    logger.warning(
                        f"Failed to get dev status detail for {issue_key} (applicationType={app_type}): {e}"
                    )
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
        return JSONToolOutput(result=commits)
