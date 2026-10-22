import asyncio
import json

import pytest
from flexmock import flexmock

from ymir.agents.tests.e2e.mr_consolidation import test_mr_consolidation as consolidation_e2e
from ymir.common.config import current_rhel_config_override, load_rhel_config

# Fixed snapshot based on templates/rhel-config.json.
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


@pytest.mark.asyncio
async def test_consolidation_snapshot_covers_backports_and_consolidation(monkeypatch, tmp_path):
    path = tmp_path / "rhel-config.json"
    path.write_text(json.dumps(RHEL_CONFIG))
    monkeypatch.setenv("RHEL_CONFIG_PATH", str(path))
    issue = {"jira_issue": "RHEL-114607"}
    case = consolidation_e2e.ConsolidationTestCase(
        "expat-rhel-9.8.0", {"backport_issues": [issue], "rhel_config_override": RHEL_10_2_OVERRIDE}
    )
    phases = []

    async def capture(phase):
        assert await load_rhel_config() == RHEL_10_2_CONFIG
        phases.append(phase)

    async def child(phase):
        await asyncio.sleep(0)
        await capture(phase)

    async def backports():
        await asyncio.gather(child("backport-one"), child("backport-two"))

    async def consolidation():
        await capture("consolidation")
        raise RuntimeError("consolidation failed")

    flexmock(case).should_receive("_run_backports").replace_with(backports)
    flexmock(case).should_receive("_run_consolidation").replace_with(consolidation)
    await case.run()

    assert set(phases[:2]) == {"backport-one", "backport-two"}
    assert phases[-1] == "consolidation"
    assert isinstance(case.error, RuntimeError)
    assert current_rhel_config_override.get() is None
    assert await load_rhel_config() == RHEL_CONFIG
