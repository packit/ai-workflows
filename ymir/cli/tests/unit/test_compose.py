import shutil
import subprocess

import pytest
from flexmock import flexmock

from ymir.cli import compose as cli_compose
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
        flexmock(subprocess).should_receive("run").once().with_args(
            ["/bin/podman", "compose", "version"],
            capture_output=True,
            check=True,
        ).and_return(flexmock(returncode=0))
        flexmock(shutil).should_receive("which").and_return("/bin/podman")

        result = detect_compose_cmd()
        assert result == ["/bin/podman", "compose"]

    def test_podman_compose_standalone(self):
        flexmock(subprocess).should_receive("run").and_raise(FileNotFoundError)
        flexmock(shutil).should_receive("which").replace_with(mock_shutil_which_podman)

        result = detect_compose_cmd()
        assert result == ["/usr/bin/podman-compose"]

    def test_docker_compose(self):
        flexmock(subprocess).should_receive("run").once().with_args(
            ["/usr/bin/docker", "compose", "version"],
            capture_output=True,
            check=True,
        ).and_return(flexmock(returncode=0))
        flexmock(shutil).should_receive("which").replace_with(mock_shutil_which_docker)

        result = detect_compose_cmd()
        assert result == ["/usr/bin/docker", "compose"]

    def test_docker_compose_standalone(self):
        def which_docker_standalone_only(cmd: str):
            if cmd == "docker":
                return "/usr/bin/docker"
            if cmd == "docker-compose":
                return "/usr/bin/docker-compose"
            return None

        flexmock(subprocess).should_receive("run").and_raise(FileNotFoundError)
        flexmock(shutil).should_receive("which").replace_with(which_docker_standalone_only)

        result = detect_compose_cmd()
        assert result == ["/usr/bin/docker-compose"]

    def test_no_runtime_raises(self):
        flexmock(subprocess).should_receive("run").and_raise(FileNotFoundError)
        flexmock(shutil).should_receive("which").and_return(None)

        with pytest.raises(RuntimeError, match="No compose tool found"):
            detect_compose_cmd()


@pytest.fixture
def _spy_subprocess_calls():
    sp_calls = []

    def _spy(*_args, **_kwargs):
        sp_calls.append((_args[0] if _args else [], _kwargs))

    flexmock(subprocess).should_receive("run").replace_with(_spy)

    return sp_calls


class TestStartServices:
    def test_calls_compose_up(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"

        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        start_services(compose_file)

        args, _ = _spy_subprocess_calls[0]
        assert "up" in args
        assert "-d" in args
        assert "--force-recreate" in args
        for svc in INFRASTRUCTURE_SERVICES:
            assert svc in args

    def test_uses_cli_profile(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        start_services(compose_file)

        args, _ = _spy_subprocess_calls[0]
        assert "--profile=cli" in args


class TestStopServices:
    def test_calls_compose_stop(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        stop_services(compose_file)
        args, _ = _spy_subprocess_calls[0]
        assert "stop" in args
        assert "mcp-gateway-cli" in args
        # We don't want to shut down trace-server
        assert "trace-server" not in args

    def test_uses_cli_profile(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        stop_services(compose_file)

        args, _ = _spy_subprocess_calls[0]
        assert "--profile=cli" in args


class TestRunAgent:
    def test_calls_compose_run_rm(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        env = {"JIRA_ISSUE": "RHEL-12345", "DRY_RUN": "true"}
        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        run_agent(compose_file, env)

        args, _ = _spy_subprocess_calls[0]
        assert "run" in args
        assert "--rm" in args
        assert "triage-cli" in args

    def test_env_vars_forwarded(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        env = {"JIRA_ISSUE": "RHEL-12345", "DRY_RUN": "true"}

        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        run_agent(compose_file, env)

        args, _ = _spy_subprocess_calls[0]
        assert "-e" in args
        assert "JIRA_ISSUE=RHEL-12345" in args
        assert "DRY_RUN=true" in args

    def test_uses_cli_profile(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        run_agent(compose_file, {})

        assert "--profile=cli" in _spy_subprocess_calls[0][0]

    def test_check_true(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        run_agent(compose_file, {})

        _, kwargs = _spy_subprocess_calls[0]
        assert kwargs["check"] is True

    def test_cwd_set_to_compose_parent(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        run_agent(compose_file, {})

        _, kwargs = _spy_subprocess_calls[0]
        assert kwargs["cwd"] == tmp_path

    def test_compose_file_passed(self, tmp_path, _spy_subprocess_calls):
        compose_file = tmp_path / "compose.yaml"
        flexmock(cli_compose).should_receive("detect_compose_cmd").and_return(["podman", "compose"])

        run_agent(compose_file, {})

        args, _ = _spy_subprocess_calls[0]
        assert "-f" in args
        idx = args.index("-f")
        assert args[idx + 1] == str(compose_file)
