import json

import pytest

from ymir.common.config import rhel_config_override
from ymir.tools.unprivileged.version_mapper import VersionMapperTool


@pytest.mark.asyncio
@pytest.mark.parametrize("upcoming_zstream", ["rhel-10.2.z", "rhel-10.1.z"])
async def test_version_mapper_uses_effective_rhel_config(monkeypatch, tmp_path, upcoming_zstream):
    config_path = tmp_path / "rhel-config.json"
    config_path.write_text(
        json.dumps(
            {
                "current_y_streams": {"10": "rhel-10.4"},
                "current_z_streams": {"10": "rhel-10.2.z"},
                "upcoming_z_streams": {"10": "rhel-10.3.z"},
            }
        )
    )
    monkeypatch.setenv("RHEL_CONFIG_PATH", str(config_path))

    with rhel_config_override(
        {
            "current_y_streams": {"10": "rhel-10.2"},
            "current_z_streams": {"10": "rhel-10.1.z"},
            "upcoming_z_streams": {"10": upcoming_zstream},
        }
    ):
        result = (await VersionMapperTool().run(input={"major_version": 10})).result

    assert result.y_stream == "rhel-10.2"
    assert result.z_stream == upcoming_zstream
    assert result.is_maintenance_version is False


@pytest.mark.asyncio
async def test_version_mapper_uses_current_zstream_for_maintenance_version(monkeypatch, tmp_path):
    config_path = tmp_path / "rhel-config.json"
    config_path.write_text(
        json.dumps(
            {
                "current_y_streams": {},
                "current_z_streams": {"8": "rhel-8.10.z"},
                "upcoming_z_streams": {},
            }
        )
    )
    monkeypatch.setenv("RHEL_CONFIG_PATH", str(config_path))

    result = (await VersionMapperTool().run(input={"major_version": 8})).result

    assert result.y_stream is None
    assert result.z_stream == "rhel-8.10.z"
    assert result.is_maintenance_version is True
