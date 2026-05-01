import datetime
import logging
import os
import re
from enum import Enum
from typing import Any
from urllib.parse import urljoin

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import (
    JSONToolOutput,
    StringToolOutput,
    Tool,
    ToolError,
    ToolRunOptions,
)
from pydantic import BaseModel, Field

from ymir.common import CVEEligibilityResult, TriageEligibility, load_rhel_config
from ymir.common.base_utils import get_jira_auth_headers
from ymir.common.constants import JIRA_SEARCH_PATH
from ymir.common.version_utils import normalize_fix_version, parse_rhel_version
from ymir.tools.constants import AIOHTTP_TIMEOUT

if os.getenv("MOCK_JIRA", "False").lower() == "true":
    from ymir.tools.privileged.aiohttp_client_session_mock import (
        aiohttpClientSessionMock as aiohttpClientSession,
    )
else:
    from aiohttp import ClientSession as aiohttpClientSession

# Jira custom field IDs
SEVERITY_CUSTOM_FIELD = "customfield_10840"
TARGET_END_CUSTOM_FIELD = "customfield_10023"
EMBARGO_CUSTOM_FIELD = "customfield_10860"

RH_EMPLOYEE_GROUP = "Red Hat Employee"

logger = logging.getLogger(__name__)


def _skip_jira_writes() -> bool:
    return os.getenv("JIRA_DRY_RUN", "False").lower() == "true"


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

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
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
                    urljoin(
                        os.getenv("JIRA_URL"),
                        f"rest/api/3/issue/{issue_key}/remotelink",
                    ),
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
                result="Skipping of setting Jira fields requested, "
                "not doing anything (this is expected, not an error)"
            )
        if os.getenv("DRY_RUN", "False").lower() == "true":
            return StringToolOutput(
                result="Dry run, not updating Jira fields (this is expected, not an error)"
            )
        if _skip_jira_writes():
            return StringToolOutput(
                result=f"JIRA_DRY_RUN is set, not updating fields "
                f"on {issue_key} (this is expected, not an error)"
            )

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
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

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
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
        if _skip_jira_writes():
            return StringToolOutput(
                result=f"JIRA_DRY_RUN is set, not adding comment "
                f"to {issue_key} (this is expected, not an error)"
            )

        jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/comment")
        logger.info(f"Connecting to JIRA API to add comment: {jira_url}")
        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            try:
                async with session.post(
                    jira_url,
                    json={
                        "body": comment,
                        **(
                            {
                                "visibility": {
                                    "type": "group",
                                    "value": RH_EMPLOYEE_GROUP,
                                }
                            }
                            if private
                            else {}
                        ),
                    },
                    headers=get_jira_auth_headers(),
                ) as response:
                    response.raise_for_status()
            except aiohttp.ClientError as e:
                raise ToolError(f"Failed to add the specified comment: {e}") from e
        return StringToolOutput(result=f"Successfully added the specified comment to {issue_key}")


def _get_maintenance_majors(rhel_config: dict) -> set[str]:
    """Major versions with a Z-stream but no Y-stream (maintenance phase)."""
    current_z_streams = rhel_config.get("current_z_streams", {})
    current_y_streams = rhel_config.get("current_y_streams", {})
    return set(current_z_streams.keys()) - set(current_y_streams.keys())


CVE_ID_PATTERN = re.compile(r"(CVE-\d{4}-\d{4,})")


def extract_cve_id(summary: str) -> str | None:
    match = CVE_ID_PATTERN.search(summary)
    return match.group(1) if match else None


async def _check_zstream_clones_shipped(
    cve_id: str, component: str, exclude_key: str
) -> tuple[bool, list[str]]:
    escaped_cve_id = cve_id.replace('"', '\\"')
    escaped_component = component.replace('"', '\\"')
    jql = (
        f'summary ~ "{escaped_cve_id}" AND component = "{escaped_component}"'
        f' AND labels = "SecurityTracking" AND key != "{exclude_key}"'
    )
    logger.info(f"Searching for Z-stream clones with JQL: {jql}")

    tool = SearchJiraIssuesTool()
    output = await tool.run(
        input={"jql": jql, "fields": ["fixVersions", "status", "resolution"], "max_results": 50}
    )
    issues = output.result

    if not issues:
        logger.info(f"No clones found for {cve_id} in component {component}, proceeding with triage")
        return (True, [])

    logger.info(f"Found {len(issues)} clone(s) for {cve_id} in component {component}")

    rhel_config = await load_rhel_config()
    current_z_streams = rhel_config.get("current_z_streams", {})
    upcoming_z_streams = rhel_config.get("upcoming_z_streams", {})
    maintenance_majors = _get_maintenance_majors(rhel_config)
    if maintenance_majors:
        logger.info(f"Maintenance-phase major versions (excluded): {sorted(maintenance_majors)}")

    relevant_z_streams = {
        v.lower()
        for streams in (current_z_streams, upcoming_z_streams)
        for major, v in streams.items()
        if major not in maintenance_majors
    }
    logger.info(f"Relevant Z-streams from config: {sorted(relevant_z_streams)}")

    any_shipped = False
    pending_keys = []
    for issue in issues:
        key = issue.get("key", "")
        fix_versions = issue.get("fields", {}).get("fixVersions", [])
        fv_names = [fv.get("name", "") for fv in fix_versions]
        has_relevant_zstream = any(fv.lower() in relevant_z_streams for fv in fv_names)
        status_name = issue.get("fields", {}).get("status", {}).get("name", "")

        resolution_name = issue.get("fields", {}).get("resolution", {})
        resolution_name = resolution_name.get("name", "") if resolution_name else ""

        if not has_relevant_zstream:
            logger.info(f"  {key}: fixVersions={fv_names} — not a relevant Z-stream, skipping")
            continue

        if status_name == "Closed" and resolution_name == "Done-Errata":
            logger.info(f"  {key}: fixVersions={fv_names}, resolution={resolution_name} — shipped")
            any_shipped = True
        elif status_name == "Closed":
            logger.info(
                f"  {key}: fixVersions={fv_names}, resolution={resolution_name} — closed but not shipped"
            )
        else:
            logger.info(f"  {key}: fixVersions={fv_names}, status={status_name} — not shipped")
            pending_keys.append(key)

    if any_shipped:
        if pending_keys:
            logger.info(
                f"At least one Z-stream clone shipped for {cve_id}, proceeding (remaining: {pending_keys})"
            )
        else:
            logger.info(f"All relevant Z-stream clones shipped for {cve_id}")
        return (True, [])

    if pending_keys:
        logger.info(f"No Z-stream clones shipped yet for {cve_id}, waiting for: {pending_keys}")
        return (False, pending_keys)

    logger.info(f"No relevant Z-stream clones found for {cve_id}, proceeding with triage")
    return (True, [])


class CheckCveTriageEligibilityToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")


class CheckCveTriageEligibilityTool(
    Tool[
        CheckCveTriageEligibilityToolInput,
        ToolRunOptions,
        JSONToolOutput[CVEEligibilityResult],
    ]
):
    name = "check_cve_triage_eligibility"
    description = """
    Analyzes if a Jira issue represents a CVE and determines when it should be processed by triage agent.

    Returns CVEEligibilityResult with eligibility:
    - IMMEDIATELY: proceed to triage
    - PENDING_DEPENDENCIES: Y-stream Critical/Important CVE waiting for Z-stream errata to ship
    - NEVER: reject (embargoed, Low/Moderate Y-stream, missing data, etc.)
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

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
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
                    eligibility=TriageEligibility.IMMEDIATELY,
                    reason="Not a CVE",
                ).model_dump()
            )

        fix_versions = fields.get("fixVersions", [])
        if not fix_versions:
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    eligibility=TriageEligibility.NEVER,
                    reason="CVE has no target release specified",
                    error="CVE has no target release specified",
                ).model_dump()
            )

        target_version = fix_versions[0].get("name", "")

        rhel_config = await load_rhel_config()
        target_version = normalize_fix_version(target_version, rhel_config)

        if re.match(r"^rhel-\d+\.\d+$", target_version.lower()):
            return await self._check_ystream_eligibility(issue_key, fields, target_version)

        embargo = fields.get(EMBARGO_CUSTOM_FIELD, {}).get("value", "")
        if embargo == "True":
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    eligibility=TriageEligibility.NEVER,
                    reason="CVE is embargoed",
                ).model_dump()
            )

        upcoming_z_streams = rhel_config.get("upcoming_z_streams", {})
        current_z_streams = rhel_config.get("current_z_streams", {})
        latest_z_streams = current_z_streams | upcoming_z_streams

        parsed = parse_rhel_version(target_version)
        is_maintenance = parsed and parsed[0] in _get_maintenance_majors(rhel_config)

        if is_maintenance:
            logger.info(f"Maintenance Z-stream CVE detected ({target_version})")
            blocker = await self._check_for_dependency_blocker(issue_key, fields, target_version)
            if blocker is not None:
                return blocker

        needs_internal_fix = False
        severity = fields.get(SEVERITY_CUSTOM_FIELD, {}).get("value", "")

        if target_version.lower() not in [v.lower() for v in latest_z_streams.values()]:
            needs_internal_fix = True
            reason = f"Z-stream CVE ({target_version}) not in latest z-streams, needs RHEL fix first"
        elif severity not in (Severity.LOW.value, Severity.MODERATE.value):
            needs_internal_fix = True
            reason = f"High severity CVE ({severity}) eligible for Z-stream, needs RHEL fix first"
        else:
            reason = "CVE eligible for Z-stream fix in CentOS Stream"

        return JSONToolOutput(
            CVEEligibilityResult(
                is_cve=True,
                eligibility=TriageEligibility.IMMEDIATELY,
                reason=reason,
                needs_internal_fix=needs_internal_fix,
            ).model_dump()
        )

    async def _check_for_dependency_blocker(
        self,
        issue_key: str,
        fields: dict[str, Any],
        target_version: str,
    ) -> JSONToolOutput[dict[str, Any]] | None:
        """Return a blocker response if no sibling clone has shipped yet, or None if clear."""
        summary = fields.get("summary", "")
        cve_id = extract_cve_id(summary)

        if not cve_id:
            logger.warning(f"Cannot extract CVE ID from summary: {summary!r}")
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    eligibility=TriageEligibility.NEVER,
                    reason=f"CVE ({target_version}): cannot extract CVE ID from summary",
                ).model_dump()
            )

        logger.info(f"Extracted CVE ID: {cve_id}")
        components = fields.get("components", [])
        component = components[0].get("name", "") if components else ""
        if not component:
            logger.warning(f"No component set on {issue_key}")
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    eligibility=TriageEligibility.NEVER,
                    reason=f"CVE {cve_id} ({target_version}): no component set on issue",
                ).model_dump()
            )

        logger.info(f"Checking clones for {cve_id}, component={component}, exclude={issue_key}")
        try:
            any_shipped, pending_keys = await _check_zstream_clones_shipped(cve_id, component, issue_key)
        except Exception as e:
            logger.warning(f"Clone dependency check failed for {cve_id}: {e}")
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    eligibility=TriageEligibility.NEVER,
                    reason=f"CVE {cve_id} ({target_version}): clone dependency check failed: {e}",
                    error=str(e),
                ).model_dump()
            )

        if any_shipped:
            logger.info(
                f"Dependency check for {issue_key} ({target_version}): "
                f"at least one clone for {cve_id} shipped"
            )
            return None

        logger.info(
            f"Dependency check for {issue_key} ({target_version}): PENDING_DEPENDENCIES "
            f"(no clones shipped yet, waiting for: {pending_keys})"
        )
        return JSONToolOutput(
            CVEEligibilityResult(
                is_cve=True,
                eligibility=TriageEligibility.PENDING_DEPENDENCIES,
                reason=f"CVE {cve_id} ({target_version}): waiting for at least one clone to ship",
                needs_internal_fix=True,
                pending_zstream_issues=pending_keys,
            ).model_dump()
        )

    async def _check_ystream_eligibility(
        self,
        issue_key: str,
        fields: dict[str, Any],
        target_version: str,
    ) -> JSONToolOutput[dict[str, Any]]:
        logger.info(f"Y-stream CVE detected ({target_version})")

        severity = fields.get(SEVERITY_CUSTOM_FIELD, {}).get("value", "")
        if severity in (Severity.LOW.value, Severity.MODERATE.value):
            logger.info(
                f"Y-stream CVE {issue_key} has {severity} severity — "
                "fix is handled via Z-stream CentOS Stream path, skipping"
            )
            return JSONToolOutput(
                CVEEligibilityResult(
                    is_cve=True,
                    eligibility=TriageEligibility.NEVER,
                    reason=(
                        f"Y-stream CVE ({target_version}, {severity} severity): "
                        "fix is handled via Z-stream CentOS Stream path"
                    ),
                ).model_dump()
            )

        logger.info(f"Severity is {severity or 'unset'}, checking Z-stream dependencies")
        blocker = await self._check_for_dependency_blocker(issue_key, fields, target_version)
        if blocker is not None:
            return blocker

        return JSONToolOutput(
            CVEEligibilityResult(
                is_cve=True,
                eligibility=TriageEligibility.IMMEDIATELY,
                reason="Y-stream CVE: at least one Z-stream clone shipped, eligible for triage",
                needs_internal_fix=True,
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
                result=f"Dry run, not changing status of {issue_key} "
                f"to {status} (this is expected, not an error)"
            )
        if _skip_jira_writes():
            return StringToolOutput(
                result=f"JIRA_DRY_RUN is set, not changing status "
                f"of {issue_key} (this is expected, not an error)"
            )

        headers = get_jira_auth_headers()
        jira_url = urljoin(os.getenv("JIRA_URL"), f"rest/api/3/issue/{issue_key}/transitions")
        logger.info(f"Connecting to JIRA API to change status: {jira_url}")

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
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
                    jira_url,
                    json={"transition": {"id": transition["id"]}},
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
            except aiohttp.ClientError as e:
                raise ToolError(f"Failed to change status of {issue_key} to {status}: {e}") from e

        return StringToolOutput(result=f"Successfully changed status of {issue_key} to {status}")


class EditJiraLabelsToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")
    labels_to_add: list[str] | None = Field(default=None, description="List of labels to add to the issue")
    labels_to_remove: list[str] | None = Field(
        default=None, description="List of labels to remove from the issue"
    )


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
        if _skip_jira_writes():
            return StringToolOutput(
                result=f"JIRA_DRY_RUN is set, not editing labels "
                f"on {issue_key} (this is expected, not an error)"
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

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
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

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
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
                    urljoin(os.getenv("JIRA_URL"), "rest/api/3/user"),
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
        description=(
            'JQL query string (e.g., \'component = "fence-agents" AND summary ~ "fix missing statuses"\')'
        ),
    )
    fields: list[str] | None = Field(
        default=None,
        description=(
            "List of fields to return (e.g., ['key', 'id', 'summary', 'fixVersions']). "
            "Defaults to key, id, summary, fixVersions."
        ),
    )
    max_results: int = Field(default=50, description="Maximum number of results to return")


class SearchJiraIssuesTool(
    Tool[SearchJiraIssuesToolInput, ToolRunOptions, JSONToolOutput[list[dict[str, Any]]]]
):
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

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
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


async def _fetch_dev_status_details(
    session: Any,
    issue_key: str,
    headers: dict,
    jira_base: str,
    summary_category: str,
    data_type: str,
) -> list[dict[str, Any]]:
    """Resolve a Jira issue ID, fetch the dev-status summary, and return
    aggregated detail records for every application type found under
    *summary_category* (e.g. ``"repository"`` or ``"pullrequest"``)."""
    issue_url = urljoin(jira_base, f"rest/api/3/issue/{issue_key}")
    try:
        async with session.get(issue_url, params={"fields": ""}, headers=headers) as response:
            response.raise_for_status()
            issue_data = await response.json()
            issue_id = issue_data["id"]
    except aiohttp.ClientError as e:
        raise ToolError(f"Failed to resolve issue ID for {issue_key}: {e}") from e

    summary_url = urljoin(jira_base, f"rest/dev-status/1.0/issue/summary?issueId={issue_id}")
    try:
        async with session.get(summary_url, headers=headers) as response:
            response.raise_for_status()
            summary_data = await response.json()
    except aiohttp.ClientError as e:
        raise ToolError(f"Failed to get dev status summary for {issue_key}: {e}") from e

    app_types = list(
        summary_data.get("summary", {}).get(summary_category, {}).get("byInstanceType", {}).keys()
    )

    details: list[dict[str, Any]] = []
    for app_type in app_types:
        detail_url = urljoin(
            jira_base,
            f"rest/dev-status/1.0/issue/detail?issueId={issue_id}"
            f"&applicationType={app_type}&dataType={data_type}",
        )
        try:
            async with session.get(detail_url, headers=headers) as response:
                response.raise_for_status()
                dev_data = await response.json()
        except aiohttp.ClientError as e:
            logger.warning(
                f"Failed to get dev-status detail for {issue_key} (applicationType={app_type}): {e}"
            )
            continue

        details.extend(dev_data.get("detail", []))

    return details


class GetJiraDevStatusToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")


class GetJiraDevStatusTool(
    Tool[GetJiraDevStatusToolInput, ToolRunOptions, JSONToolOutput[list[dict[str, Any]]]]
):
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

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            details = await _fetch_dev_status_details(
                session,
                issue_key,
                headers,
                jira_base,
                summary_category="repository",
                data_type="repository",
            )

        commits = [
            {
                "url": commit.get("url", ""),
                "message": commit.get("message", ""),
                "repository_url": repo.get("url", ""),
            }
            for detail in details
            for repo in detail.get("repositories", [])
            for commit in repo.get("commits", [])
        ]

        logger.info(f"Found {len(commits)} commits in development status for {issue_key}")
        return JSONToolOutput(result=commits)


class GetJiraPullRequestsToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")


class GetJiraPullRequestsTool(
    Tool[
        GetJiraPullRequestsToolInput,
        ToolRunOptions,
        JSONToolOutput[list[dict[str, Any]]],
    ]
):
    name = "get_jira_pull_requests"
    description = """
    Gets pull/merge requests linked to a Jira issue via the dev-status API.
    Returns a list of pull request dicts with keys: id, name, status, url,
    source, destination, repositoryName, repositoryUrl.
    """
    input_schema = GetJiraPullRequestsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetJiraPullRequestsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        issue_key = tool_input.issue_key
        headers = get_jira_auth_headers()
        jira_base = os.getenv("JIRA_URL")
        logger.info(f"Fetching pull requests for {issue_key}")

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            details = await _fetch_dev_status_details(
                session,
                issue_key,
                headers,
                jira_base,
                summary_category="pullrequest",
                data_type="pullrequest",
            )

        pull_requests: list[dict[str, Any]] = []
        for detail in details:
            pull_requests.extend(detail.get("pullRequests", []))

        logger.info(f"Found {len(pull_requests)} pull requests for {issue_key}")
        return JSONToolOutput(result=pull_requests)


class SetPreliminaryTestingToolInput(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. RHEL-12345)")
    value: PreliminaryTesting = Field(description="Value to set for Preliminary Testing field")
    comment: str | None = Field(default=None, description="Optional comment to add to the issue")


class SetPreliminaryTestingTool(Tool[SetPreliminaryTestingToolInput, ToolRunOptions, StringToolOutput]):
    name = "set_preliminary_testing"
    description = """
    Updates the Preliminary Testing custom field on a Jira issue.
    Optionally adds a comment at the same time.
    """
    input_schema = SetPreliminaryTestingToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "jira", self.name],
            creator=self,
        )

    async def _resolve_field_id(self, session: Any, headers: dict) -> str:
        jira_base = os.getenv("JIRA_URL")
        url = urljoin(jira_base, "rest/api/3/field")
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            fields = await response.json()
        for field in fields:
            if field["name"] == "Preliminary Testing":
                return field["id"]
        raise ToolError("Could not find 'Preliminary Testing' custom field in Jira")

    async def _run(
        self,
        tool_input: SetPreliminaryTestingToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        issue_key = tool_input.issue_key
        value = tool_input.value
        comment = tool_input.comment

        if os.getenv("DRY_RUN", "False").lower() == "true":
            return StringToolOutput(
                result=f"Dry run, not setting Preliminary Testing "
                f"on {issue_key} (this is expected, not an error)"
            )

        headers = get_jira_auth_headers()
        jira_base = os.getenv("JIRA_URL")

        async with aiohttpClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            field_id = await self._resolve_field_id(session, headers)

            body: dict[str, Any] = {
                "fields": {field_id: {"value": str(value.value)}},
            }
            if comment is not None:
                body["update"] = {
                    "comment": [{"add": {"body": comment}}],
                }

            url = urljoin(jira_base, f"rest/api/2/issue/{issue_key}")
            try:
                async with session.put(url, json=body, headers=headers) as response:
                    response.raise_for_status()
            except aiohttp.ClientError as e:
                raise ToolError(f"Failed to set Preliminary Testing on {issue_key}: {e}") from e

        return StringToolOutput(
            result=f"Successfully set Preliminary Testing to {value.value} on {issue_key}"
        )
