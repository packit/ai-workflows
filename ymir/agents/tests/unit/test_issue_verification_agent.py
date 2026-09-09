"""Unit tests for issue verification agent shared rules fetching."""

import json

import pytest
from flexmock import flexmock

from ymir.agents import issue_verification_agent as iv_agent
from ymir.agents.issue_verification_agent import _fetch_shared_rules


@pytest.mark.asyncio
async def test_fetch_shared_rules_preserves_order():
    async def _mock_run_tool(name, available_tools=None, **kwargs):
        if name == "get_shared_rules":
            return json.dumps(["b", "a", "c"])
        rule_set = kwargs["file_path"].split("/")[0]
        return f"content-{rule_set}"

    flexmock(iv_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    result = await _fetch_shared_rules([], "some-package")

    assert result == (
        "--- Shared rules (b) ---\ncontent-b\n\n"
        "--- Shared rules (a) ---\ncontent-a\n\n"
        "--- Shared rules (c) ---\ncontent-c"
    )


@pytest.mark.asyncio
async def test_fetch_shared_rules_isolates_failures():
    async def _mock_run_tool(name, available_tools=None, **kwargs):
        if name == "get_shared_rules":
            return json.dumps(["good", "bad"])
        rule_set = kwargs["file_path"].split("/")[0]
        if rule_set == "bad":
            raise TimeoutError("boom")
        return f"content-{rule_set}"

    flexmock(iv_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    result = await _fetch_shared_rules([], "some-package")

    assert result == "--- Shared rules (good) ---\ncontent-good"
