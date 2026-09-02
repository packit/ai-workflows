"""Unit tests for issue verification agent shared rules fetching."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from ymir.agents.issue_verification_agent import _fetch_shared_rules


@pytest.mark.asyncio
async def test_fetch_shared_rules_preserves_order():
    async def fake_run_tool(name, available_tools=None, **kwargs):
        if name == "get_shared_rules":
            return json.dumps(["b", "a", "c"])
        rule_set = kwargs["file_path"].split("/")[0]
        return f"content-{rule_set}"

    with patch("ymir.agents.issue_verification_agent.run_tool", new=AsyncMock(side_effect=fake_run_tool)):
        result = await _fetch_shared_rules([], "some-package")

    assert result == (
        "--- Shared rules (b) ---\ncontent-b\n\n"
        "--- Shared rules (a) ---\ncontent-a\n\n"
        "--- Shared rules (c) ---\ncontent-c"
    )


@pytest.mark.asyncio
async def test_fetch_shared_rules_isolates_failures():
    async def fake_run_tool(name, available_tools=None, **kwargs):
        if name == "get_shared_rules":
            return json.dumps(["good", "bad"])
        rule_set = kwargs["file_path"].split("/")[0]
        if rule_set == "bad":
            raise TimeoutError("boom")
        return f"content-{rule_set}"

    with patch("ymir.agents.issue_verification_agent.run_tool", new=AsyncMock(side_effect=fake_run_tool)):
        result = await _fetch_shared_rules([], "some-package")

    assert result == "--- Shared rules (good) ---\ncontent-good"
