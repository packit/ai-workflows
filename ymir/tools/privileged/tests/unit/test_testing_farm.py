"""Unit tests for ReserveTestingFarmMachineTool and GetTestingFarmReservationDetailsTool."""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from beeai_framework.tools import ToolError
from flexmock import flexmock
from pydantic import ValidationError

from ymir.tools.privileged import testing_farm as tf_module
from ymir.tools.privileged.testing_farm import (
    _SSH_KEY_PATH,
    CancelTestingFarmRequestTool,
    CancelTestingFarmRequestToolInput,
    CopyFilesToRemoteTool,
    CopyFilesToRemoteToolInput,
    GetTestingFarmReservationDetailsTool,
    GetTestingFarmReservationDetailsToolInput,
    ReserveTestingFarmMachineTool,
    RunRemoteCommandTool,
)

SAMPLE_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey user@host"
GATEWAY_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGatewayKey gateway@mcp"


@pytest.fixture(autouse=True)
def _mock_gateway_ssh_key():
    """Mock _ensure_gateway_ssh_key so tests don't generate real keys."""
    with patch.object(tf_module, "_ensure_gateway_ssh_key", return_value=GATEWAY_SSH_KEY):
        yield


@pytest.mark.asyncio
async def test_reserve_machine_dry_run(monkeypatch):
    """DRY_RUN=true returns a fake ID without calling the API."""
    monkeypatch.setenv("DRY_RUN", "true")

    out = await ReserveTestingFarmMachineTool().run(
        input={
            "compose": "RHEL-9.8.0-Nightly",
            "ssh_public_key": SAMPLE_SSH_KEY,
        }
    )
    result = out.result
    assert result["id"] == "dry-run-reservation"
    assert "Dry run" in result["message"]
    assert "RHEL-9.8.0-Nightly" in result["message"]
    assert "x86_64" in result["message"]
    assert "60m" in result["message"]


@pytest.mark.asyncio
async def test_reserve_machine_request_body(monkeypatch):
    """Verify the request body structure matches the reserve-system pattern."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    # Clear the cached headers so the monkeypatched env var is picked up
    tf_module._testing_farm_headers.cache_clear()

    captured = {}

    def fake_post(path, json):
        captured["path"] = path
        captured["body"] = json
        return {"id": "req-001"}

    flexmock(tf_module).should_receive("_testing_farm_api_post").replace_with(fake_post).once()

    await ReserveTestingFarmMachineTool().run(
        input={
            "compose": "RHEL-9.8.0-Nightly",
            "arch": "aarch64",
            "duration_minutes": 120,
            "ssh_public_key": SAMPLE_SSH_KEY,
        }
    )

    assert captured["path"] == "requests"
    body = captured["body"]

    # Top-level test section uses fmf with plan name
    assert body["test"] == {
        "fmf": {
            "url": "https://gitlab.com/testing-farm/tests",
            "ref": "main",
            "name": "/testing-farm/reserve",
        }
    }
    assert len(body["environments"]) == 1

    env = body["environments"][0]
    assert env["arch"] == "aarch64"
    assert env["os"] == {"compose": "RHEL-9.8.0-Nightly"}

    # Duration must be passed as a string
    assert env["variables"]["TF_RESERVATION_DURATION"] == "120"

    # SSH key must be the gateway's own key, base64-encoded
    expected_b64 = base64.b64encode(GATEWAY_SSH_KEY.encode()).decode()
    assert env["secrets"]["TF_RESERVATION_AUTHORIZED_KEYS_BASE64"] == expected_b64

    # No tmt extra_args in environment — standalone reservation uses test.fmf
    assert "tmt" not in env

    # Security group rules allow SSH from anywhere
    ingress = env["settings"]["provisioning"]["security_group_rules_ingress"]
    assert len(ingress) == 1
    assert ingress[0]["protocol"] == "tcp"
    assert ingress[0]["port_min"] == 22
    assert ingress[0]["port_max"] == 22
    assert ingress[0]["cidr"] == "0.0.0.0/0"

    # Pipeline timeout must be set
    assert body["settings"]["pipeline"]["timeout"] == 720


@pytest.mark.asyncio
async def test_reserve_machine_returns_request_id(monkeypatch):
    """The tool returns the request ID from the API response."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    flexmock(tf_module).should_receive("_testing_farm_api_post").and_return(
        {"id": "abc-123"}
    ).once()

    out = await ReserveTestingFarmMachineTool().run(
        input={
            "compose": "RHEL-9.8.0-Nightly",
            "ssh_public_key": SAMPLE_SSH_KEY,
        }
    )
    assert out.result == {"id": "abc-123"}


@pytest.mark.asyncio
async def test_reserve_machine_default_arch(monkeypatch):
    """Default arch is x86_64 when not specified."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    captured = {}

    def fake_post(path, json):
        captured["body"] = json
        return {"id": "req-002"}

    flexmock(tf_module).should_receive("_testing_farm_api_post").replace_with(fake_post).once()

    await ReserveTestingFarmMachineTool().run(
        input={
            "compose": "RHEL-9.8.0-Nightly",
            "ssh_public_key": SAMPLE_SSH_KEY,
        }
    )

    env = captured["body"]["environments"][0]
    assert env["arch"] == "x86_64"


@pytest.mark.asyncio
async def test_reserve_machine_ssh_key_encoding(monkeypatch):
    """The gateway's SSH key is properly base64-encoded (agent-provided key is ignored)."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    captured = {}

    def fake_post(path, json):
        captured["body"] = json
        return {"id": "req-003"}

    flexmock(tf_module).should_receive("_testing_farm_api_post").replace_with(fake_post).once()

    agent_key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQ+test user@host"

    await ReserveTestingFarmMachineTool().run(
        input={
            "compose": "Fedora-41",
            "ssh_public_key": agent_key,
        }
    )

    stored_b64 = captured["body"]["environments"][0]["secrets"][
        "TF_RESERVATION_AUTHORIZED_KEYS_BASE64"
    ]

    # Gateway key is used, not the agent-provided key
    decoded = base64.b64decode(stored_b64).decode()
    assert decoded == GATEWAY_SSH_KEY
    assert decoded != agent_key


# -- GetTestingFarmReservationDetailsTool tests --


@pytest.mark.asyncio
async def test_reservation_details_dry_run(monkeypatch):
    """DRY_RUN=true returns complete state with dry-run-host."""
    monkeypatch.setenv("DRY_RUN", "true")

    out = await GetTestingFarmReservationDetailsTool().run(
        input={"request_id": "req-dry-001"}
    )
    assert out.result == {"state": "complete", "ssh_connection": "root@dry-run-host"}


@pytest.mark.asyncio
async def test_reservation_details_running_with_guest(monkeypatch):
    """When pipeline.log has guest IP and task #1 started, ssh_connection is extracted."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    api_response = {
        "state": "running",
        "run": {"artifacts": "https://artifacts.testing-farm.io/abc123"},
    }

    pipeline_log = (
        "some log output\n"
        "Guest is ready at root@10.0.0.1\n"
        "more output\n"
        "execute task #1\n"
    )

    flexmock(tf_module).should_receive("_testing_farm_api_get").with_args(
        "requests/req-100"
    ).and_return(api_response).once()

    mock_resp = flexmock(ok=True, text=pipeline_log)
    flexmock(requests).should_receive("get").with_args(
        "https://artifacts.testing-farm.io/abc123/pipeline.log", timeout=30
    ).and_return(mock_resp).once()

    out = await GetTestingFarmReservationDetailsTool().run(
        input={"request_id": "req-100"}
    )
    assert out.result == {"state": "running", "ssh_connection": "root@10.0.0.1"}


@pytest.mark.asyncio
async def test_reservation_details_pending_then_canceled(monkeypatch):
    """When state is pending then transitions to canceled, returns canceled."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    flexmock(tf_module).should_receive("_testing_farm_api_get").with_args(
        "requests/req-200"
    ).and_return({"state": "pending"}).and_return({"state": "canceled"})

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    out = await GetTestingFarmReservationDetailsTool().run(
        input={"request_id": "req-200"}
    )
    assert out.result == {"state": "canceled", "ssh_connection": "not-yet-available"}


@pytest.mark.asyncio
async def test_reservation_details_running_no_task_then_ready(monkeypatch):
    """When task #1 hasn't started on first poll, polls again until ready."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    api_response = {
        "state": "running",
        "run": {"artifacts": "https://artifacts.testing-farm.io/abc123"},
    }

    log_not_ready = "Guest is ready at root@10.0.0.1\nprovisioning still in progress\n"
    log_ready = "Guest is ready at root@10.0.0.1\nexecute task #1\n"

    flexmock(tf_module).should_receive("_testing_farm_api_get").with_args(
        "requests/req-300"
    ).and_return(api_response)

    flexmock(requests).should_receive("get").with_args(
        "https://artifacts.testing-farm.io/abc123/pipeline.log", timeout=30
    ).and_return(flexmock(ok=True, text=log_not_ready)).and_return(
        flexmock(ok=True, text=log_ready)
    )

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    out = await GetTestingFarmReservationDetailsTool().run(
        input={"request_id": "req-300"}
    )
    assert out.result == {"state": "running", "ssh_connection": "root@10.0.0.1"}


@pytest.mark.asyncio
async def test_reservation_details_multihost_pattern(monkeypatch):
    """Multihost pattern with 'primary address:' is also matched."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    api_response = {
        "state": "running",
        "run": {"artifacts": "https://artifacts.testing-farm.io/def456"},
    }

    pipeline_log = (
        "[guest1]   primary address: 10.0.0.99\n"
        "execute task #1\n"
    )

    flexmock(tf_module).should_receive("_testing_farm_api_get").with_args(
        "requests/req-400"
    ).and_return(api_response).once()

    mock_resp = flexmock(ok=True, text=pipeline_log)
    flexmock(requests).should_receive("get").with_args(
        "https://artifacts.testing-farm.io/def456/pipeline.log", timeout=30
    ).and_return(mock_resp).once()

    out = await GetTestingFarmReservationDetailsTool().run(
        input={"request_id": "req-400"}
    )
    assert out.result == {"state": "running", "ssh_connection": "root@10.0.0.99"}


@pytest.mark.asyncio
async def test_reservation_details_multihost_realistic_tag(monkeypatch):
    """Multihost pattern with realistic TF tag containing dashes, dots, colons, slashes."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    api_response = {
        "state": "running",
        "run": {"artifacts": "https://artifacts.testing-farm.io/ghi789"},
    }

    pipeline_log = (
        "[RHEL-10.0-Nightly:x86_64:/testing-farm/reserve]         primary address: 10.31.8.81\n"
        "execute task #1\n"
    )

    flexmock(tf_module).should_receive("_testing_farm_api_get").with_args(
        "requests/req-500"
    ).and_return(api_response).once()

    mock_resp = flexmock(ok=True, text=pipeline_log)
    flexmock(requests).should_receive("get").with_args(
        "https://artifacts.testing-farm.io/ghi789/pipeline.log", timeout=30
    ).and_return(mock_resp).once()

    out = await GetTestingFarmReservationDetailsTool().run(
        input={"request_id": "req-500"}
    )
    assert out.result == {"state": "running", "ssh_connection": "root@10.31.8.81"}


# -- CancelTestingFarmRequestTool tests --


@pytest.mark.asyncio
async def test_cancel_request_dry_run(monkeypatch):
    """DRY_RUN=true returns cancelled=True with message, no API call."""
    monkeypatch.setenv("DRY_RUN", "true")

    out = await CancelTestingFarmRequestTool().run(
        input={"request_id": "req-cancel-dry"}
    )
    result = out.result
    assert result["cancelled"] is True
    assert result["request_id"] == "req-cancel-dry"
    assert "Dry run" in result["message"]
    assert "req-cancel-dry" in result["message"]


@pytest.mark.asyncio
async def test_cancel_request_calls_delete(monkeypatch):
    """The tool calls _testing_farm_api_delete with the correct path."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    flexmock(tf_module).should_receive("_testing_farm_api_delete").with_args(
        "requests/req-500"
    ).once()

    await CancelTestingFarmRequestTool().run(
        input={"request_id": "req-500"}
    )


@pytest.mark.asyncio
async def test_cancel_request_returns_confirmation(monkeypatch):
    """The tool returns cancelled=True and the request_id."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    flexmock(tf_module).should_receive("_testing_farm_api_delete").with_args(
        "requests/req-600"
    ).once()

    out = await CancelTestingFarmRequestTool().run(
        input={"request_id": "req-600"}
    )
    assert out.result == {"cancelled": True, "request_id": "req-600"}


# -- RunRemoteCommandTool tests --


def _make_fake_process(stdout=b"", stderr=b"", returncode=0):
    """Create a mock asyncio subprocess process."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@pytest.mark.asyncio
async def test_run_remote_command_dry_run(monkeypatch):
    """DRY_RUN=true returns fake result without executing SSH."""
    monkeypatch.setenv("DRY_RUN", "true")

    out = await RunRemoteCommandTool().run(
        input={
            "ssh_host": "root@10.0.0.1",
            "command": "uname -r",
        }
    )
    result = out.result
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["exit_code"] == 0
    assert "Dry run" in result["message"]
    assert "uname -r" in result["message"]
    assert "root@10.0.0.1" in result["message"]


@pytest.mark.asyncio
async def test_run_remote_command_success(monkeypatch):
    """Successful SSH command returns stdout/stderr/exit_code and uses correct SSH args."""
    monkeypatch.delenv("DRY_RUN", raising=False)

    fake_proc = _make_fake_process(
        stdout=b"5.14.0-362.el9.x86_64\n",
        stderr=b"",
        returncode=0,
    )

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_proc) as mock_exec:
        out = await RunRemoteCommandTool().run(
            input={
                "ssh_host": "root@10.0.0.1",
                "command": "uname -r",
            }
        )

        # Verify SSH args include -i for gateway key
        mock_exec.assert_called_once_with(
            "ssh",
            "-i", str(_SSH_KEY_PATH),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "root@10.0.0.1",
            "uname -r",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    result = out.result
    assert result["stdout"] == "5.14.0-362.el9.x86_64\n"
    assert result["stderr"] == ""
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_run_remote_command_nonzero_exit(monkeypatch):
    """Non-zero exit code is passed through, not treated as an error."""
    monkeypatch.delenv("DRY_RUN", raising=False)

    fake_proc = _make_fake_process(
        stdout=b"",
        stderr=b"ls: cannot access '/nope': No such file or directory\n",
        returncode=1,
    )

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_proc):
        out = await RunRemoteCommandTool().run(
            input={
                "ssh_host": "root@10.0.0.1",
                "command": "ls /nope",
            }
        )

    result = out.result
    assert result["exit_code"] == 1
    assert result["stdout"] == ""
    assert "No such file or directory" in result["stderr"]


# -- CopyFilesToRemoteTool tests --


@pytest.mark.asyncio
async def test_copy_files_dry_run(monkeypatch):
    """DRY_RUN=true returns fake result without executing SSH/SCP."""
    monkeypatch.setenv("DRY_RUN", "true")

    out = await CopyFilesToRemoteTool().run(
        input={
            "ssh_host": "root@10.0.0.1",
            "local_paths": ["/tmp/test.sh", "/tmp/data.txt"],
        }
    )
    result = out.result
    assert result["copied"] is True
    assert result["remote_dir"] == "/tmp/reproducer"
    assert result["files"] == ["/tmp/test.sh", "/tmp/data.txt"]
    assert "Dry run" in result["message"]
    assert "root@10.0.0.1" in result["message"]


@pytest.mark.asyncio
async def test_copy_files_success(monkeypatch):
    """Successful copy runs mkdir then scp, returns correct result."""
    monkeypatch.delenv("DRY_RUN", raising=False)

    mkdir_proc = _make_fake_process(stdout=b"", stderr=b"", returncode=0)
    scp_proc = _make_fake_process(stdout=b"", stderr=b"", returncode=0)

    call_count = 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mkdir_proc
        return scp_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec) as mock_exec:
        out = await CopyFilesToRemoteTool().run(
            input={
                "ssh_host": "root@10.0.0.1",
                "local_paths": ["/tmp/test.sh"],
                "remote_dir": "/opt/work",
            }
        )

        # Two calls: mkdir and scp
        assert mock_exec.call_count == 2

        # First call: ssh mkdir (with -i for gateway key)
        mkdir_call = mock_exec.call_args_list[0]
        mkdir_args = mkdir_call[0]
        assert mkdir_args[0] == "ssh"
        assert str(_SSH_KEY_PATH) in mkdir_args
        assert "StrictHostKeyChecking=no" in mkdir_args
        assert "root@10.0.0.1" in mkdir_args
        assert "mkdir" in mkdir_args
        assert "-p" in mkdir_args
        assert "/opt/work" in mkdir_args

        # Second call: scp (with -i for gateway key)
        scp_call = mock_exec.call_args_list[1]
        scp_args = scp_call[0]
        assert scp_args[0] == "scp"
        assert str(_SSH_KEY_PATH) in scp_args
        assert "-r" in scp_args
        assert "/tmp/test.sh" in scp_args
        assert "root@10.0.0.1:/opt/work" in scp_args

    result = out.result
    assert result["copied"] is True
    assert result["remote_dir"] == "/opt/work"
    assert result["files"] == ["/tmp/test.sh"]


# -- Error-path / validation tests --


@pytest.mark.asyncio
async def test_run_remote_command_timeout_kills_process(monkeypatch):
    """Timeout triggers proc.kill() and raises ToolError."""
    monkeypatch.delenv("DRY_RUN", raising=False)

    fake_proc = _make_fake_process()
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    with (
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_proc),
        pytest.raises(ToolError, match="timed out"),
    ):
        await RunRemoteCommandTool().run(
            input={"ssh_host": "root@10.0.0.1", "command": "sleep 999", "timeout": 5}
        )

    fake_proc.kill.assert_called_once()
    fake_proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_copy_files_timeout_kills_process(monkeypatch):
    """CopyFilesToRemoteTool timeout triggers proc.kill() and raises ToolError."""
    monkeypatch.delenv("DRY_RUN", raising=False)

    fake_proc = _make_fake_process()
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    with (
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_proc),
        pytest.raises(ToolError, match="timed out"),
    ):
        await CopyFilesToRemoteTool().run(
                input={
                    "ssh_host": "root@10.0.0.1",
                    "local_paths": ["/tmp/test.sh"],
                    "timeout": 5,
                }
            )

    fake_proc.kill.assert_called_once()
    fake_proc.wait.assert_awaited_once()


@pytest.mark.parametrize("bad_host", [
    "root@host; rm -rf /",
    "root@host && curl evil.com",
    "user@host|cat /etc/passwd",
    "root@",
])
def test_ssh_host_pattern_rejects_injection(bad_host):
    """ssh_host pattern blocks shell metacharacters."""
    with pytest.raises(ValidationError):
        CopyFilesToRemoteToolInput(
            ssh_host=bad_host,
            local_paths=["/tmp/test.sh"],
        )


def test_local_paths_guard_rejects_outside_allowed_dirs():
    """local_paths guard rejects paths outside /git-repos/ and /tmp/."""
    with pytest.raises(ValidationError, match="not under an allowed directory"):
        CopyFilesToRemoteToolInput(
            ssh_host="root@10.0.0.1",
            local_paths=["/etc/passwd"],
        )


def test_local_paths_guard_rejects_traversal():
    """local_paths guard rejects directory traversal attempts."""
    with pytest.raises(ValidationError, match="not under an allowed directory"):
        CopyFilesToRemoteToolInput(
            ssh_host="root@10.0.0.1",
            local_paths=["/tmp/../../etc/passwd"],
        )


@pytest.mark.parametrize("bad_dir", [
    "/tmp/foo; curl evil.com",
    "/tmp/foo && rm -rf /",
    "/tmp/$(whoami)",
    "/tmp/`id`",
])
def test_remote_dir_pattern_rejects_injection(bad_dir):
    """remote_dir pattern blocks shell metacharacters."""
    with pytest.raises(ValidationError):
        CopyFilesToRemoteToolInput(
            ssh_host="root@10.0.0.1",
            local_paths=["/tmp/test.sh"],
            remote_dir=bad_dir,
        )


@pytest.mark.parametrize("bad_id", [
    "../../admin",
    "req-123/../../secrets",
    "req 123",
    "req;drop",
])
def test_request_id_pattern_rejects_traversal(bad_id):
    """request_id pattern blocks path traversal and special characters."""
    with pytest.raises(ValidationError):
        GetTestingFarmReservationDetailsToolInput(request_id=bad_id)
    with pytest.raises(ValidationError):
        CancelTestingFarmRequestToolInput(request_id=bad_id)


@pytest.mark.asyncio
async def test_reservation_details_transient_http_error_retries(monkeypatch):
    """Transient 503 during polling is retried instead of aborting."""
    monkeypatch.setenv("TESTING_FARM_API_TOKEN", "fake-token")
    tf_module._testing_farm_headers.cache_clear()

    mock_response_503 = MagicMock()
    mock_response_503.status_code = 503

    call_count = 0

    def fake_get(path, params=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            err = requests.HTTPError(response=mock_response_503)
            raise err
        return {"state": "complete"}

    flexmock(tf_module).should_receive("_testing_farm_api_get").replace_with(fake_get)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    out = await GetTestingFarmReservationDetailsTool().run(
        input={"request_id": "req-transient"}
    )
    assert out.result["state"] == "complete"
    assert call_count == 2
