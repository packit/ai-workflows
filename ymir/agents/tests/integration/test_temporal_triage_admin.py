"""Exercise the triage administration commands."""

import json
from argparse import Namespace

import pytest
from flexmock import flexmock

from ymir.agents import temporal_triage_admin as admin


@pytest.mark.asyncio
async def test_recover_coordination_uses_configured_control_queue(monkeypatch, capsys):
    monkeypatch.setenv("TEMPORAL_TRIAGE_CONTROL_QUEUE", "recovery-queue")
    client = object()

    async def connect():
        return client

    async def recover(recovery_client, queue):
        assert recovery_client is client
        assert queue == "recovery-queue"
        return {"RHEL-1": "accepted"}

    flexmock(admin).should_receive("connect_triage_ingress").replace_with(connect).once()
    flexmock(admin).should_receive("recover_package_coordination").replace_with(recover).once()

    await admin.main(Namespace(command="recover-coordination"))

    assert json.loads(capsys.readouterr().out) == {"RHEL-1": "accepted"}
