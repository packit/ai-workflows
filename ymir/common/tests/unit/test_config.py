import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.types import RequestParams

from ymir.common.config import load_rhel_config, rhel_config_override

# Fixed snapshot based on templates/rhel-config.json, rather than a changing runtime config.
RHEL_CONFIG = {
    "current_y_streams": {"9": "rhel-9.10", "10": "rhel-10.4"},
    "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.8.z", "10": "rhel-10.2.z"},
    "upcoming_z_streams": {"9": "rhel-9.9.z", "10": "rhel-10.3.z"},
    "internal_repos_host": "https://internal.host",
}
RHEL_10_2_OVERRIDE = {
    "current_y_streams": {"10": "rhel-10.2"},
    "current_z_streams": {"10": "rhel-10.1.z"},
    "upcoming_z_streams": {"10": "rhel-10.2.z"},
}
RHEL_10_2_CONFIG = {
    "current_y_streams": {"9": "rhel-9.10", "10": "rhel-10.2"},
    "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.8.z", "10": "rhel-10.1.z"},
    "upcoming_z_streams": {"9": "rhel-9.9.z", "10": "rhel-10.2.z"},
    "internal_repos_host": "https://internal.host",
}


def test_load_rhel_config_without_mcp(monkeypatch, tmp_path):
    config_path = tmp_path / "rhel-config.json"
    config_path.write_text(json.dumps(RHEL_CONFIG))
    monkeypatch.setenv("RHEL_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("ALLOW_RHEL_CONFIG_OVERRIDE", "true")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, json, os, sys; "
            "sys.modules['mcp'] = None; "
            "from ymir.common.config import load_rhel_config; "
            "from pathlib import Path; "
            "expected = json.loads(Path(os.environ['RHEL_CONFIG_PATH']).read_text()); "
            "assert asyncio.run(load_rhel_config()) == expected",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_load_rhel_config_applies_agent_and_gateway_overrides(monkeypatch, tmp_path):
    config_path = tmp_path / "rhel-config.json"
    config_path.write_text(json.dumps(RHEL_CONFIG))
    monkeypatch.setenv("RHEL_CONFIG_PATH", str(config_path))

    with rhel_config_override(RHEL_10_2_OVERRIDE):
        assert await load_rhel_config() == RHEL_10_2_CONFIG

    # The gateway permission is deliberately independent of Jira mocking.
    monkeypatch.setenv("ALLOW_RHEL_CONFIG_OVERRIDE", "true")
    token = request_ctx.set(SimpleNamespace(meta=RequestParams.Meta(rhel_config_override=RHEL_10_2_OVERRIDE)))
    try:
        assert await load_rhel_config() == RHEL_10_2_CONFIG
    finally:
        request_ctx.reset(token)

    assert await load_rhel_config() == RHEL_CONFIG


@pytest.mark.asyncio
async def test_gateway_ignores_metadata_overrides_when_not_allowed(monkeypatch, tmp_path):
    config_path = tmp_path / "rhel-config.json"
    config_path.write_text(json.dumps(RHEL_CONFIG))
    monkeypatch.setenv("RHEL_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("ALLOW_RHEL_CONFIG_OVERRIDE", "false")
    token = request_ctx.set(SimpleNamespace(meta=RequestParams.Meta(rhel_config_override=RHEL_10_2_OVERRIDE)))
    try:
        assert await load_rhel_config() == RHEL_CONFIG
    finally:
        request_ctx.reset(token)
