"""
Jira read-only queries for Golang rebuild agent.

Write operations (comments, labels, transitions) use agents/tasks.py MCP helpers.
This module handles only domain-specific query/extraction logic.
"""

import logging
import os
import re
from typing import Any

from jira import JIRA
from jira.exceptions import JIRAError

from ymir.agents.golang_rebuild.constants import COMPONENT_VALID_STATUSES, GOLANG_CVE_FIXED_STATUSES
from ymir.agents.golang_rebuild.models import GolangCVEInfo
from ymir.agents.golang_rebuild.utils import extract_cves_from_text, extract_rhel_version_from_text
from ymir.common.constants import GOLANG_REBUILD_QUEUE_LABEL
from ymir.common.version_utils import get_short_version, parse_rhel_version

logger = logging.getLogger(__name__)


class GolangJiraQueries:
    """
    Read-only Jira queries for golang CVE rebuild discovery.

    Uses the `jira` Python library for direct API access.
    """

    MAX_RESULTS_PER_PAGE = 500
    API_TIMEOUT = 90

    def __init__(self, jira_url: str | None = None, config: dict | None = None):
        self.jira_url = jira_url or os.getenv("JIRA_URL", "https://redhat.atlassian.net")
        self.config = config or {}

        jira_email = os.getenv("JIRA_EMAIL") or os.getenv("JIRA_USERNAME")
        jira_token = os.getenv("JIRA_API_TOKEN") or os.getenv("JIRA_PASSWORD")

        if not jira_email or not jira_token:
            raise ValueError(
                "Jira credentials not found. Set JIRA_EMAIL and JIRA_API_TOKEN "
                "environment variables (or source ~/.rh-jira-mcp.env)"
            )

        options = {
            "server": self.jira_url,
            "rest_api_version": "3",
            "agile_rest_api_version": "latest",
        }
        self.jira = JIRA(
            options=options,
            basic_auth=(jira_email, jira_token),
            max_retries=3,
            timeout=self.API_TIMEOUT,
        )
        logger.info(f"Connected to Jira at {self.jira_url}")

    def _issue_to_dict(self, issue) -> dict[str, Any]:
        """Convert JIRA issue object to dictionary."""
        return {
            "key": issue.key,
            "id": issue.id,
            "fields": {
                "summary": str(getattr(issue.fields, "summary", "")),
                "description": str(getattr(issue.fields, "description", "") or ""),
                "status": {"name": str(getattr(issue.fields.status, "name", ""))}
                if hasattr(issue.fields, "status")
                else {},
                "labels": list(getattr(issue.fields, "labels", [])),
                "components": [{"name": str(c.name)} for c in getattr(issue.fields, "components", [])],
            },
        }

    def search_issues(
        self, jql: str, fields: list[str] | None = None, max_results: int | None = None
    ) -> list[dict[str, Any]]:
        """Search for issues using JQL query."""
        if fields is None:
            fields_list = ["key", "summary", "labels", "status", "description", "components"]
        elif isinstance(fields, list):
            fields_list = fields
        else:
            fields_list = fields.split(",")

        logger.debug(f"Searching Jira: {jql}")

        try:
            import requests

            url = f"{self.jira_url}/rest/api/3/search/jql"
            auth = self.jira._session.auth
            payload = {
                "jql": jql,
                "maxResults": max_results or self.MAX_RESULTS_PER_PAGE,
                "fields": fields_list,
            }
            response = requests.post(url, json=payload, auth=auth, timeout=self.API_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            result = []
            for issue_data in data.get("issues", []):
                issue = self.jira.issue(issue_data["key"], fields=",".join(fields_list))
                result.append(self._issue_to_dict(issue))

            logger.info(f"Found {len(result)} issues")
            return result

        except (JIRAError, requests.HTTPError) as e:
            logger.error(f"Jira search failed: {e}")
            raise

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Get a single issue by key."""
        try:
            issue = self.jira.issue(issue_key, fields="key,summary,labels,status,description,components")
            return self._issue_to_dict(issue)
        except JIRAError as e:
            logger.error(f"Failed to get issue {issue_key}: {e}")
            raise

    def find_golang_cve_tickets(self) -> list[dict[str, Any]]:
        """Find Golang CVE tickets in the queue (with golang-rebuild-queue label)."""
        query_template = self.config.get("jira", {}).get("queries", {}).get("golang_queue")
        if not query_template:
            status_list = ", ".join(f'"{s}"' for s in GOLANG_CVE_FIXED_STATUSES)
            query_template = (
                f"project = RHEL AND labels = {GOLANG_REBUILD_QUEUE_LABEL} "
                f"AND component = golang AND status IN ({status_list}) "
                f"AND labels = CVE ORDER BY created DESC"
            )
        jql = query_template.format(queue_label=GOLANG_REBUILD_QUEUE_LABEL)
        logger.info(f"Searching for Golang CVE tickets: {jql}")
        return self.search_issues(jql)

    def find_dependent_tickets(self, cve_id: str, rhel_version: str) -> list[dict[str, Any]]:
        """Find component tickets affected by a specific CVE."""
        short_version = get_short_version(rhel_version) or rhel_version

        query_template = self.config.get("jira", {}).get("queries", {}).get("dependent_tickets")
        if not query_template:
            status_list = ", ".join(f'"{s}"' for s in COMPONENT_VALID_STATUSES)
            query_template = (
                f'project = RHEL AND summary ~ "{{cve}}" '
                f'AND (summary ~ "{{rhel_version}}" OR summary ~ "{{short_version}}") '
                f"AND status IN ({status_list}) "
                f"ORDER BY component ASC"
            )

        jql = query_template.format(cve=cve_id, rhel_version=rhel_version, short_version=short_version)
        logger.info(f"Searching for dependent tickets: {jql}")
        return self.search_issues(jql)

    def extract_golang_cve_info(self, issue: dict[str, Any]) -> GolangCVEInfo | None:
        """Extract Golang CVE information from a Jira ticket."""
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        description = fields.get("description", "")
        status = fields.get("status", {}).get("name", "")

        cve_ids = extract_cves_from_text(summary + " " + description)
        if not cve_ids:
            logger.warning(f"No CVE IDs found in ticket {issue.get('key')}")
            return None

        rhel_version = extract_rhel_version_from_text(summary + " " + description)
        if not rhel_version:
            logger.warning(f"No RHEL version found in ticket {issue.get('key')}")
            return None

        parsed = parse_rhel_version(rhel_version)
        if parsed is None:
            logger.warning(f"Invalid RHEL version format: {rhel_version}")
            return None

        _major, _minor, is_zstream = parsed
        if not is_zstream:
            logger.info(f"Skipping y-stream ticket {issue.get('key')}: {rhel_version}")
            return None

        # Extract golang version
        golang_match = re.search(
            r"(?:golang-?|go)(\d+\.\d+\.\d+)", summary + " " + description, re.IGNORECASE
        )
        golang_version = golang_match.group(1) if golang_match else "unknown"

        return GolangCVEInfo(
            ticket_key=issue.get("key"),
            cve_ids=cve_ids,
            rhel_version=rhel_version,
            golang_version=golang_version,
            status=status,
            is_zstream=is_zstream,
            summary=summary,
            description=description,
        )

    def get_issue_comments(self, issue_key: str) -> list[dict]:
        """
        Get comments from a Jira ticket.

        Returns list of dicts with "id", "body", "author", "created" keys.
        Ordered oldest-first (Jira default).
        """
        try:
            comments = self.jira.comments(issue_key)
            return [
                {
                    "id": str(c.id),
                    "body": str(getattr(c, "body", "")),
                    "author": str(getattr(c.author, "displayName", "")) if hasattr(c, "author") else "",
                    "created": str(getattr(c, "created", "")),
                }
                for c in comments
            ]
        except JIRAError as e:
            logger.warning(f"Failed to get comments for {issue_key}: {e}")
            return []

    def check_label_exists(self, issue_key: str, label: str) -> bool:
        """Check if a specific label exists on a ticket."""
        try:
            issue = self.jira.issue(issue_key, fields="labels")
            labels = list(getattr(issue.fields, "labels", []))
            return label in labels
        except JIRAError as e:
            logger.warning(f"Failed to check labels for {issue_key}: {e}")
            return False
