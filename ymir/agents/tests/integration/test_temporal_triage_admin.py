"""Exercise lossless transfer of a legacy list entry."""

import json

import pytest
from flexmock import flexmock

from ymir.agents import temporal_triage_admin as admin


@pytest.mark.asyncio
async def test_transfer_retries_an_uncertain_submission_with_same_id():
    queue = "triage_queue"
    records = [json.dumps({"metadata": {"issue": "RHEL-1", "force_cve_triage": True}}).encode()]
    accepted = []

    async def peek(name, index):
        assert name == queue and index == -1
        return records[-1] if records else None

    async def remove(name, count, item):
        assert name == queue and count == -1 and records[-1] == item
        records.pop()
        return 1

    async def submit(_client, intent):
        assert intent.issue == "RHEL-1" and intent.force_cve_triage
        accepted.append(intent.request_id)
        if len(accepted) == 1:
            raise TimeoutError("Accepted response was lost")
        return "accepted"

    redis = flexmock(lindex=peek, lrem=remove)
    flexmock(admin).should_receive("submit_triage_intent").replace_with(submit)
    with pytest.raises(TimeoutError):
        await admin.transfer_queue(redis, object(), queue)
    assert len(records) == 1
    assert await admin.transfer_queue(redis, object(), queue) == 1
    assert records == [] and accepted[0] == accepted[1]
