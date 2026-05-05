"""
Unit tests for Brew Client (async)
"""

from unittest.mock import patch

import pytest

from ymir.agents.golang_rebuild.brew_client import BrewClient
from ymir.agents.golang_rebuild.models import BuildResult


@pytest.fixture
def brew_client():
    return BrewClient(config={"brew": {"poll_interval": 1, "max_wait_time": 10}})


@pytest.fixture
def mock_repo_path(tmp_path):
    repo = tmp_path / "buildah"
    repo.mkdir()
    return repo


class TestBrewClient:
    def test_extract_task_id_created_task(self, brew_client):
        output = "Created task: 123456789\nTask info: https://brewweb..."
        assert brew_client._extract_task_id(output) == "123456789"

    def test_extract_task_id_task_url(self, brew_client):
        output = "Task info: https://brewweb.engineering.redhat.com/brew/taskinfo?taskID=987654321"
        assert brew_client._extract_task_id(output) == "987654321"

    def test_extract_task_id_not_found(self, brew_client):
        assert brew_client._extract_task_id("Some random output") is None

    @pytest.mark.asyncio
    @patch("ymir.agents.golang_rebuild.brew_client._run_command")
    async def test_scratch_build(self, mock_run, brew_client, mock_repo_path):
        mock_run.return_value = (0, "Created task: 123456789\n", "")
        task_id = await brew_client.scratch_build(mock_repo_path, "rhel-9.7.0-candidate")
        assert task_id == "123456789"
        args = mock_run.call_args[0][0]
        assert "rhpkg" in args
        assert "scratch-build" in args

    @pytest.mark.asyncio
    @patch("ymir.agents.golang_rebuild.brew_client._run_command")
    async def test_final_build(self, mock_run, brew_client, mock_repo_path):
        mock_run.return_value = (0, "Created task: 987654321\n", "")
        task_id = await brew_client.final_build(mock_repo_path, "rhel-9.7.0-candidate")
        assert task_id == "987654321"
        args = mock_run.call_args[0][0]
        assert "rhpkg" in args
        assert "build" in args

    @pytest.mark.asyncio
    async def test_get_task_info(self, brew_client):
        with patch.object(brew_client, "_run_brew_command") as mock_brew:
            mock_brew.return_value = (
                0,
                "Task: 123456789\nState: closed\nResult: success\nBuild: buildah-1.33.13-3.2.el9_7 (12345)\n",
                "",
            )
            info = await brew_client.get_task_info("123456789")
            assert info["Task"] == "123456789"
            assert info["State"] == "closed"
            assert info["Result"] == "success"

    @pytest.mark.asyncio
    async def test_get_task_state_success(self, brew_client):
        with patch.object(brew_client, "get_task_info") as mock_info:
            mock_info.return_value = {"State": "closed", "Result": "success"}
            state, result = await brew_client.get_task_state("123456789")
            assert state == "closed"
            assert result == "success"

    @pytest.mark.asyncio
    async def test_get_task_state_fail(self, brew_client):
        with patch.object(brew_client, "get_task_info") as mock_info:
            mock_info.return_value = {"State": "closed", "Result": "failed"}
            state, result = await brew_client.get_task_state("123456789")
            assert state == "closed"
            assert result == "fail"

    @pytest.mark.asyncio
    async def test_get_task_state_running(self, brew_client):
        with patch.object(brew_client, "get_task_info") as mock_info:
            mock_info.return_value = {"State": "open"}
            state, result = await brew_client.get_task_state("123456789")
            assert state == "open"
            assert result is None

    @pytest.mark.asyncio
    async def test_is_task_finished_success(self, brew_client):
        with patch.object(brew_client, "get_task_state") as mock_state:
            mock_state.return_value = ("closed", "success")
            is_finished, result = await brew_client.is_task_finished("123456789")
            assert is_finished is True
            assert result == "success"

    @pytest.mark.asyncio
    async def test_is_task_finished_running(self, brew_client):
        with patch.object(brew_client, "get_task_state") as mock_state:
            mock_state.return_value = ("open", None)
            is_finished, result = await brew_client.is_task_finished("123456789")
            assert is_finished is False
            assert result is None

    @pytest.mark.asyncio
    async def test_build_and_wait_scratch(self, brew_client, mock_repo_path):
        with (
            patch.object(brew_client, "scratch_build") as mock_scratch,
            patch.object(brew_client, "wait_for_task") as mock_wait,
        ):
            mock_scratch.return_value = "123456789"
            mock_wait.return_value = BuildResult(
                task_id="123456789", nvr="buildah-1.33.13-3.2.el9_7", success=True, state="success"
            )
            result = await brew_client.build_and_wait(mock_repo_path, "rhel-9.7.0-candidate", scratch=True)
            assert result.success is True
            mock_scratch.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_and_wait_final(self, brew_client, mock_repo_path):
        with (
            patch.object(brew_client, "final_build") as mock_final,
            patch.object(brew_client, "wait_for_task") as mock_wait,
        ):
            mock_final.return_value = "987654321"
            mock_wait.return_value = BuildResult(
                task_id="987654321", nvr="buildah-1.33.13-3.2.el9_7", success=True, state="success"
            )
            result = await brew_client.build_and_wait(mock_repo_path, "rhel-9.7.0-candidate", scratch=False)
            assert result.success is True
            mock_final.assert_called_once()

    @pytest.mark.asyncio
    @patch("ymir.agents.golang_rebuild.brew_client._run_command")
    async def test_verify_kerberos_auth_valid(self, mock_run, brew_client):
        mock_run.return_value = (0, "", "")
        assert await brew_client.verify_kerberos_auth() is True

    @pytest.mark.asyncio
    @patch("ymir.agents.golang_rebuild.brew_client._run_command")
    async def test_verify_kerberos_auth_invalid(self, mock_run, brew_client):
        mock_run.return_value = (1, "", "")
        assert await brew_client.verify_kerberos_auth() is False
