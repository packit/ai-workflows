"""Tests for no-patch retries through Temporal."""

import pytest
from flexmock import flexmock

from ymir.common.constants import JiraLabels
from ymir.sweep.no_patch import NoPatchSweep
from ymir.sweep.tests.unit.conftest import make_issue


@pytest.mark.asyncio
async def test_no_patch_preserves_triage_only_and_leaves_labels_to_temporal():
    issue = make_issue(
        key="RHEL-1",
        labels=[JiraLabels.TRIAGE_ONLY.value, JiraLabels.YMIR_POSTPONED_NO_PATCH.value],
    )

    async def blocked():
        return [issue]

    flexmock(NoPatchSweep).should_receive("_get_blocked_issues_async").replace_with(blocked)
    from ymir.sweep import base

    submitted = []

    async def submit(client, intent):
        submitted.append(intent)
        return "accepted"

    flexmock(base).should_receive("submit_triage_intent").replace_with(submit)
    flexmock(base).should_receive("remove_issue_label").never()
    flexmock(base).should_receive("add_issue_label").never()

    assert (await NoPatchSweep().run(object()))["unblocked"] == 1
    [intent] = submitted
    assert intent.issue == issue.key
    assert intent.triage_only and intent.retry_label == JiraLabels.YMIR_POSTPONED_NO_PATCH.value
