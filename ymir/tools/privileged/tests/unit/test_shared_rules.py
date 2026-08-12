import json
import time
from unittest.mock import patch

import pytest
from beeai_framework.tools import ToolError

from ymir.tools.privileged.shared_rules import SharedRulesTool

SAMPLE_REGISTRY = {
    "python": ["python-requests", "python-urllib3", "python-cryptography"],
    "perl": ["perl-Module-Build", "perl-Test-Simple"],
    "autotools": ["curl", "python-cryptography"],
}


def _fresh_tool():
    return SharedRulesTool(options={"working_directory": None})


def _tool_with_cached_registry(registry):
    tool = _fresh_tool()
    tool._registry_cache = registry
    tool._registry_fetched_at = time.monotonic()
    return tool


@pytest.mark.asyncio
async def test_package_found_in_one_rule_set():
    tool = _tool_with_cached_registry(SAMPLE_REGISTRY)
    result = await tool.run({"package": "python-requests"})
    assert json.loads(result.result) == ["python"]


@pytest.mark.asyncio
async def test_package_found_in_multiple_rule_sets():
    tool = _tool_with_cached_registry(SAMPLE_REGISTRY)
    result = await tool.run({"package": "python-cryptography"})
    assert sorted(json.loads(result.result)) == ["autotools", "python"]


@pytest.mark.asyncio
async def test_package_not_found():
    tool = _tool_with_cached_registry(SAMPLE_REGISTRY)
    result = await tool.run({"package": "nonexistent-package"})
    assert json.loads(result.result) == []


@pytest.mark.asyncio
async def test_empty_registry():
    tool = _tool_with_cached_registry({})
    result = await tool.run({"package": "python-requests"})
    assert json.loads(result.result) == []


@pytest.mark.asyncio
async def test_registry_cached_as_none():
    tool = _fresh_tool()
    tool._registry_cache = None
    tool._registry_fetched_at = time.monotonic()
    result = await tool.run({"package": "python-requests"})
    assert json.loads(result.result) == []


@pytest.mark.asyncio
async def test_non_list_values_skipped():
    tool = _tool_with_cached_registry({"python": ["pkg-a"], "bad_entry": "not-a-list"})
    result = await tool.run({"package": "pkg-a"})
    assert json.loads(result.result) == ["python"]


@pytest.mark.asyncio
async def test_caching_multiple_lookups_same_registry():
    tool = _tool_with_cached_registry(SAMPLE_REGISTRY)

    result1 = await tool.run({"package": "python-requests"})
    assert json.loads(result1.result) == ["python"]

    result2 = await tool.run({"package": "curl"})
    assert json.loads(result2.result) == ["autotools"]

    assert tool._registry_fetched_at > 0
    assert tool._registry_cache is SAMPLE_REGISTRY


@pytest.mark.asyncio
@patch.object(SharedRulesTool, "_fetch_registry")
async def test_transient_error_not_cached(mock_fetch):
    mock_fetch.side_effect = [TimeoutError("connection timed out"), SAMPLE_REGISTRY]
    tool = _fresh_tool()

    with pytest.raises(ToolError, match="Failed to fetch shared rules registry"):
        await tool.run({"package": "python-requests"})

    result = await tool.run({"package": "python-requests"})
    assert json.loads(result.result) == ["python"]
    assert mock_fetch.call_count == 2
