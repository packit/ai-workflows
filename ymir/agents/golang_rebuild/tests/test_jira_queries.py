"""
Unit tests for Jira Queries (read-only operations)
"""

from unittest.mock import MagicMock, patch

import pytest

from ymir.agents.golang_rebuild.jira_queries import GolangJiraQueries


@pytest.fixture
def jira_queries():
    """Create GolangJiraQueries with mocked JIRA client (no real credentials needed)."""
    with (
        patch.dict("os.environ", {"JIRA_EMAIL": "test@test.com", "JIRA_API_TOKEN": "fake-token"}),
        patch("ymir.agents.golang_rebuild.jira_queries.JIRA") as mock_jira_cls,
    ):
        mock_jira_cls.return_value = MagicMock()
        return GolangJiraQueries(jira_url="https://test.jira.com")


@pytest.fixture
def sample_golang_ticket():
    return {
        "key": "RHEL-158645",
        "fields": {
            "summary": "CVE-2025-12345 CVE-2025-67890 for RHEL 9.7.z - golang security update",
            "description": "Security update for golang-1.25.8 to fix multiple CVEs",
            "status": {"name": "Release Pending"},
            "labels": ["CVE", "golang-rebuild-queue"],
            "components": [{"name": "golang"}],
        },
    }


class TestGolangJiraQueries:
    def test_extract_golang_cve_info_success(self, jira_queries, sample_golang_ticket):
        info = jira_queries.extract_golang_cve_info(sample_golang_ticket)
        assert info is not None
        assert info.ticket_key == "RHEL-158645"
        assert "CVE-2025-12345" in info.cve_ids
        assert "CVE-2025-67890" in info.cve_ids
        assert info.rhel_version == "rhel-9.7.z"
        assert info.is_zstream is True
        assert info.golang_version == "1.25.8"

    def test_extract_golang_cve_info_ystream_skipped(self, jira_queries):
        ystream_ticket = {
            "key": "RHEL-200000",
            "fields": {
                "summary": "CVE-2025-11111 for RHEL 9.8 - golang update",
                "description": "Feature release update",
                "status": {"name": "Done"},
                "labels": ["CVE"],
            },
        }
        info = jira_queries.extract_golang_cve_info(ystream_ticket)
        assert info is None

    def test_extract_golang_cve_info_no_cve(self, jira_queries):
        ticket = {
            "key": "RHEL-111111",
            "fields": {
                "summary": "Regular golang update for RHEL 9.7.z",
                "description": "No CVE mentioned",
                "status": {"name": "New"},
            },
        }
        info = jira_queries.extract_golang_cve_info(ticket)
        assert info is None

    @patch.object(GolangJiraQueries, "search_issues")
    def test_find_golang_cve_tickets(self, mock_search, jira_queries, sample_golang_ticket):
        mock_search.return_value = [sample_golang_ticket]
        tickets = jira_queries.find_golang_cve_tickets()
        assert len(tickets) == 1
        assert tickets[0]["key"] == "RHEL-158645"
        mock_search.assert_called_once()

    @patch.object(GolangJiraQueries, "search_issues")
    def test_find_dependent_tickets(self, mock_search, jira_queries):
        component_ticket = {
            "key": "RHEL-149580",
            "fields": {
                "summary": "CVE-2025-12345 for RHEL 9.7.z - buildah rebuild",
                "components": [{"name": "buildah"}],
            },
        }
        mock_search.return_value = [component_ticket]
        tickets = jira_queries.find_dependent_tickets("CVE-2025-12345", "rhel-9.7.z")
        assert len(tickets) == 1
        assert tickets[0]["key"] == "RHEL-149580"

    def test_get_issue(self, jira_queries):
        mock_issue = MagicMock()
        mock_issue.key = "RHEL-158645"
        mock_issue.id = "12345"
        mock_issue.fields.summary = "Test summary"
        mock_issue.fields.description = "Test description"
        mock_issue.fields.status.name = "New"
        mock_issue.fields.labels = ["CVE"]
        mock_issue.fields.components = []

        jira_queries.jira.issue.return_value = mock_issue

        result = jira_queries.get_issue("RHEL-158645")
        assert result["key"] == "RHEL-158645"
        assert result["fields"]["summary"] == "Test summary"
