"""Exercise manual branch holds through triage workflow."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ymir.agents import tasks
from ymir.agents.rebase_consolidation import get_rebase_primary_issue
from ymir.common.constants import JiraLabels


@pytest.mark.parametrize(
    "reference",
    [
        "RHEL-200",
        "https://redhat.atlassian.net/browse/RHEL-200#icft=RHEL-200",
        '<custom data-type="smartlink">https://redhat.atlassian.net/browse/RHEL-200</custom>',
    ],
)
def test_primary_reference_is_read_after_marker(reference):
    details = {
        "fields": {
            "comment": {
                "comments": [{"body": f"RHEL-100: Queued for triage as potential sibling of {reference}"}]
            }
        }
    }
    assert get_rebase_primary_issue(details) == "RHEL-200"


def test_primary_reference_supports_jira_inline_card():
    details = {
        "fields": {
            "comment": {
                "comments": [
                    {
                        "body": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Queued for triage as potential sibling of ",
                                        },
                                        {
                                            "type": "inlineCard",
                                            "attrs": {"url": "https://example.com/browse/RHEL-200"},
                                        },
                                    ],
                                }
                            ],
                        }
                    }
                ]
            }
        }
    }
    assert get_rebase_primary_issue(details) == "RHEL-200"


def test_primary_reference_keeps_full_issue_key():
    details = {
        "fields": {"comment": {"comments": [{"body": "Queued for triage as potential sibling of RHEL-2001"}]}}
    }
    assert get_rebase_primary_issue(details) == "RHEL-2001"


@asynccontextmanager
async def _gateway(*_args, **_kwargs):
    yield []


@pytest.mark.asyncio
async def test_manual_hold_labels_group_and_keeps_primary_result_comment(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway")
    labels = AsyncMock()
    comments = AsyncMock()
    monkeypatch.setattr(tasks, "set_jira_labels", labels)
    monkeypatch.setattr(tasks, "comment_in_jira", comments)
    monkeypatch.setattr(tasks, "mcp_tools", _gateway)

    await tasks.handle_manual_branch_creation_required(
        tasks.ManualBranchCreationRequired("bash", "rhel-10.3", ["RHEL-1", "RHEL-2"]),
        agent_type="Triage",
        triaged_label=JiraLabels.TRIAGED_REBASE.value,
        dry_run=False,
        user_triggered=False,
        primary_comment="Full triage result",
    )

    assert [call.kwargs["jira_issue"] for call in labels.await_args_list] == ["RHEL-2", "RHEL-1"]
    assert comments.await_args_list[0].kwargs["comment_text"] == "Full triage result"
    assert "create the branch manually" in comments.await_args_list[1].kwargs["comment_text"]
