from unittest.mock import MagicMock, patch

import pytest

from ymir.cli.compose import (
    INFRASTRUCTURE_SERVICES,
    detect_compose_cmd,
    run_agent,
    start_services,
    stop_services,
)


def mock_shutil_which_podman(cmd: str):
    """Behaves as `which` in environment with installed `podman` and `podman-compose`."""
    if cmd == "podman":
        return "/bin/podman"
    if cmd == "podman-compose":
        return "/usr/bin/podman-compose"
    return None


def mock_shutil_which_docker(cmd: str):
    """Behaves as `which` in environment with only `docker` and `docker-compose`."""
    if cmd == "docker":
        return "/usr/bin/docker"
    if cmd == "docker-compose":
        return "/usr/bin/docker-compose"
    return None


class TestDetectComposeCmd:
    def test_podman_compose(self):
        with patch("subprocess.run") as mock_run, patch("shutil.which", return_value="/bin/podman"):
            mock_run.return_value = MagicMock(returncode=0)
            result = detect_compose_cmd()
        assert result == ["/bin/podman", "compose"]
        mock_run.assert_called_once_with(
            ["/bin/podman", "compose", "version"],
            capture_output=True,
            check=True,
        )

    def test_podman_compose_standalone(self):
        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("shutil.which", side_effect=mock_shutil_which_podman),
        ):
            result = detect_compose_cmd()
        assert result == ["/usr/bin/podman-compose"]

    def test_docker_compose(self):
        with (
            patch("subprocess.run") as mock_run,
            patch("shutil.which", side_effect=mock_shutil_which_docker),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = detect_compose_cmd()
        assert result == ["/usr/bin/docker", "compose"]
        mock_run.assert_called_once_with(
            ["/usr/bin/docker", "compose", "version"],
            capture_output=True,
            check=True,
        )

    def test_docker_compose_standalone(self):
        def which_docker_standalone_only(cmd: str):
            if cmd == "docker":
                return "/usr/bin/docker"
            if cmd == "docker-compose":
                return "/usr/bin/docker-compose"
            return None

        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("shutil.which", side_effect=which_docker_standalone_only),
        ):
            result = detect_compose_cmd()
        assert result == ["/usr/bin/docker-compose"]

    def test_no_runtime_raises(self):
        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="No compose tool found"),
        ):
            detect_compose_cmd()


class TestStartServices:
    def test_calls_compose_up(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            start_services(compose_file)
        args = mock_run.call_args[0][0]
        assert "up" in args
        assert "-d" in args
        assert "--force-recreate" in args
        for svc in INFRASTRUCTURE_SERVICES:
            assert svc in args

    def test_uses_cli_profile(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            start_services(compose_file)
        args = mock_run.call_args[0][0]
        assert "--profile=cli" in args


class TestStopServices:
    def test_calls_compose_stop(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            stop_services(compose_file)
        args = mock_run.call_args[0][0]
        assert "stop" in args
        assert "mcp-gateway-cli" in args
        # We don't want to shut down trace-server
        assert "trace-server" not in args

    def test_uses_cli_profile(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            stop_services(compose_file)
        args = mock_run.call_args[0][0]
        assert "--profile=cli" in args


class TestRunAgent:
    def test_calls_compose_run_rm(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        env = {"JIRA_ISSUE": "RHEL-12345", "DRY_RUN": "true"}
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            run_agent(compose_file, env)
        args = mock_run.call_args[0][0]
        assert "run" in args
        assert "--rm" in args
        assert "triage-cli" in args

    def test_env_vars_forwarded(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        env = {"JIRA_ISSUE": "RHEL-12345", "DRY_RUN": "true"}
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            run_agent(compose_file, env)
        args = mock_run.call_args[0][0]
        assert "-e" in args
        assert "JIRA_ISSUE=RHEL-12345" in args
        assert "DRY_RUN=true" in args

    def test_uses_cli_profile(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            run_agent(compose_file, {})
        args = mock_run.call_args[0][0]
        assert "--profile=cli" in args

    def test_check_true(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            run_agent(compose_file, {})
        assert mock_run.call_args[1]["check"] is True

    def test_cwd_set_to_compose_parent(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            run_agent(compose_file, {})
        assert mock_run.call_args[1]["cwd"] == tmp_path

    def test_compose_file_passed(self, tmp_path):
        compose_file = tmp_path / "compose.yaml"
        with (
            patch("ymir.cli.compose.detect_compose_cmd", return_value=["podman", "compose"]),
            patch("subprocess.run") as mock_run,
        ):
            run_agent(compose_file, {})
        args = mock_run.call_args[0][0]
        assert "-f" in args
        idx = args.index("-f")
        assert args[idx + 1] == str(compose_file)
