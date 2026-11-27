"""Exercise the triage administration commands."""

import json
from argparse import Namespace

import pytest
from flexmock import flexmock

from ymir.agents import temporal_triage_admin as admin


@pytest.mark.asyncio
@pytest.mark.parametrize(("user_triggered", "triggered_by"), [(False, "retry"), (True, "user")])
async def test_submit_uses_requested_triage_policy(user_triggered, triggered_by):
    client = object()

    async def connect():
        return client

    async def submit(connection, intent, *, wait, retain_result):
        assert connection is client and wait and retain_result
        assert intent.triggered_by == triggered_by
        assert intent.retry_label is None
        return "accepted"

    flexmock(admin).should_receive("connect_triage_ingress").replace_with(connect).once()
    flexmock(admin).should_receive("submit_triage_intent").replace_with(submit).once()

    await admin.main(
        Namespace(
            command="submit",
            issue="RHEL-1",
            request_id="fixture",
            dry_run=True,
            user_triggered=user_triggered,
            force_cve_triage=False,
            triage_only=False,
            wait=True,
        )
    )


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
