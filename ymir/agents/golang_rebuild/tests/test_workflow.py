"""
Unit tests for Workflow Orchestrator (async)
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from ymir.agents.golang_rebuild.models import (
    ComponentRebuildInfo,
    GolangCVEInfo,
    RebuildStatus,
)
from ymir.agents.golang_rebuild.workflow import GolangRebuildWorkflow


@pytest.fixture
def workflow():
    """Create workflow with mocked dependencies (no real credentials needed)."""
    with (
        patch("ymir.agents.golang_rebuild.workflow.load_golang_config") as mock_config,
        patch("ymir.agents.golang_rebuild.workflow.GolangJiraQueries"),
        patch("ymir.agents.golang_rebuild.workflow.GitClient"),
        patch("ymir.agents.golang_rebuild.workflow.BrewClient"),
    ):
        mock_config.return_value = {
            "user": {"name": "Test User", "email": "test@redhat.com"},
            "workspace": {"base_path": "/tmp/RHEL"},
            "rhel_versions": {
                "9.7.z": {
                    "branch": "rhel-9.7.0",
                    "build_target": "rhel-9.7.0-candidate",
                }
            },
            "component_filter": {"enabled": False},
        }
        return GolangRebuildWorkflow(dry_run=True)


@pytest.fixture
def sample_cve_info():
    return GolangCVEInfo(
        ticket_key="RHEL-158645",
        cve_ids=["CVE-2025-12345", "CVE-2025-67890"],
        rhel_version="rhel-9.7.z",
        golang_version="1.25.8",
        status="Release Pending",
        is_zstream=True,
    )


@pytest.fixture
def sample_component_info():
    return ComponentRebuildInfo(
        component="buildah",
        ticket_key="RHEL-149580",
        rhel_version="rhel-9.7.z",
        cve_ids=["CVE-2025-12345"],
        golang_version="1.25.8",
        branch="rhel-9.7.0",
        build_target="rhel-9.7.0-candidate",
    )


class TestGolangRebuildWorkflow:
    @pytest.mark.asyncio
    async def test_process_golang_cve_ticket(self, workflow, sample_cve_info):
        workflow.jira_queries.get_issue.return_value = {
            "key": "RHEL-158645",
            "fields": {"summary": "CVE-2025-12345 for RHEL 9.7.z"},
        }
        workflow.jira_queries.extract_golang_cve_info.return_value = sample_cve_info

        with (
            patch.object(workflow, "_find_all_dependent_tickets") as mock_find,
            patch.object(workflow, "process_component_rebuild") as mock_process,
        ):
            mock_find.return_value = [
                {
                    "key": "RHEL-149580",
                    "fields": {
                        "components": [{"name": "buildah"}],
                        "summary": "CVE-2025-12345 - buildah",
                    },
                },
            ]
            mock_process.return_value = True

            summary = await workflow.process_golang_cve_ticket("RHEL-158645")

            assert summary.golang_ticket == "RHEL-158645"
            assert summary.components_processed == 1
            assert summary.components_succeeded == 1

    @pytest.mark.asyncio
    async def test_process_component_rebuild(self, workflow, sample_component_info):
        with (
            patch.object(workflow, "_process_rebuild") as mock_rebuild,
            patch("ymir.agents.golang_rebuild.workflow.get_rhel_version_config") as mock_vc,
            patch("ymir.agents.golang_rebuild.workflow._import_tasks") as mock_tasks,
        ):
            mock_vc.return_value = {
                "branch": "rhel-9.7.0",
                "build_target": "rhel-9.7.0-candidate",
            }
            mock_rebuild.return_value = True
            mock_tasks.return_value = MagicMock()

            success = await workflow.process_component_rebuild(sample_component_info)
            assert success is True
            mock_rebuild.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_rebuild_spec_error(self, workflow, sample_component_info, tmp_path):
        with (
            patch("ymir.agents.golang_rebuild.workflow._import_tasks") as mock_tasks_fn,
            patch("ymir.agents.golang_rebuild.workflow.SpecFile") as mock_spec_cls,
        ):
            mock_tasks = MagicMock()
            mock_tasks.fork_and_prepare_dist_git = AsyncMock(
                return_value=(tmp_path, "branch", "fork_url", None)
            )
            mock_tasks_fn.return_value = mock_tasks
            mock_spec_cls.find_spec_file.return_value = None

            workflow.gateway_tools = [MagicMock()]
            workflow.jira_queries.get_issue_comments = Mock(return_value=[])

            success = await workflow._process_rebuild(sample_component_info)
            assert success is False
            assert sample_component_info.status == RebuildStatus.FAILED
            assert "No .spec file found" in sample_component_info.error_message

    def test_extract_component_name_from_components(self, workflow):
        issue = {
            "key": "RHEL-149580",
            "fields": {"components": [{"name": "buildah"}], "summary": "Some summary"},
        }
        assert workflow._extract_component_name(issue) == "buildah"

    def test_extract_component_name_from_summary(self, workflow):
        issue = {
            "key": "RHEL-149580",
            "fields": {"components": [], "summary": "CVE-2025-12345 - podman rebuild"},
        }
        assert workflow._extract_component_name(issue) == "podman"

    def test_find_all_dependent_tickets_dedupe(self, workflow, sample_cve_info):
        workflow.jira_queries.find_dependent_tickets = Mock(
            side_effect=[
                [{"key": "RHEL-149580"}, {"key": "RHEL-147034"}],
                [{"key": "RHEL-149580"}, {"key": "RHEL-150000"}],
            ]
        )
        tickets = workflow._find_all_dependent_tickets(sample_cve_info)
        assert len(tickets) == 3
        keys = [t["key"] for t in tickets]
        assert "RHEL-149580" in keys
        assert "RHEL-147034" in keys
        assert "RHEL-150000" in keys

    @pytest.mark.asyncio
    async def test_process_queue(self, workflow):
        workflow.jira_queries.find_golang_cve_tickets = Mock(
            return_value=[{"key": "RHEL-158645"}, {"key": "RHEL-158646"}]
        )
        with patch.object(workflow, "process_golang_cve_ticket") as mock_process:
            mock_process.return_value = Mock(
                golang_ticket="RHEL-158645",
                components_succeeded=1,
                components_failed=0,
            )
            summaries = await workflow.process_queue(max_tickets=2)
            assert len(summaries) == 2
            assert mock_process.call_count == 2
