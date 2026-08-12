"""Unit tests for reproducer agent label and comment helpers."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ymir.agents.reproducer_agent import (
    _determine_comment_resolution,
    _determine_result_label,
    _needs_merge_request,
    _prepare_reproducer_branch,
    _resolve_test_dir,
    _should_finalize_jira,
)
from ymir.common.base_utils import check_subprocess
from ymir.common.constants import JiraLabels
from ymir.common.models import MergeRequestDetails, ReproducerOutputSchema


def _output(**overrides) -> ReproducerOutputSchema:
    data = {
        "jira_issue": "RHEL-12345",
        "success": True,
        "reproducer_type": "bug",
        "package": "libfoo",
        "pass_fail_criteria": "exit 0 on fixed",
        "summary": "ok",
    }
    data.update(overrides)
    return ReproducerOutputSchema(**data)


@pytest.mark.parametrize(
    ("overrides", "expected_label", "expected_resolution"),
    [
        ({}, JiraLabels.REPRODUCER_CREATED, "reproduced"),
        (
            {"success": False, "not_reproducible_reason": "race"},
            JiraLabels.REPRODUCER_NOT_REPRODUCIBLE,
            "not-reproducible",
        ),
        (
            {"success": False, "test_already_exists": True},
            JiraLabels.REPRODUCER_ALREADY_EXISTS,
            "already-exists",
        ),
        ({"success": False}, JiraLabels.REPRODUCER_FAILED, "failed"),
    ],
)
def test_determine_result_label_and_comment_resolution(overrides, expected_label, expected_resolution):
    result = _output(**overrides)
    assert _determine_result_label(result) == expected_label
    assert _determine_comment_resolution(result) == expected_resolution


def test_already_exists_takes_precedence_over_success():
    result = _output(success=True, test_already_exists=True)
    assert _determine_result_label(result) == JiraLabels.REPRODUCER_ALREADY_EXISTS
    assert _determine_comment_resolution(result) == "already-exists"


def test_adapted_existing_uses_created_label():
    result = _output(success=True, test_already_exists=True, adapted_existing=True)
    assert _determine_result_label(result) == JiraLabels.REPRODUCER_CREATED
    assert _determine_comment_resolution(result) == "adapted-existing"


def test_should_finalize_jira_false_for_retryable_error():
    assert _should_finalize_jira(_output(success=False, retryable_error=True)) is False
    assert _should_finalize_jira(_output(success=False, lock_deferred=True)) is False
    assert _should_finalize_jira(_output(success=False)) is True
    assert _should_finalize_jira(_output(success=True)) is True


def test_needs_merge_request():
    assert _needs_merge_request(_output(success=True)) is True
    assert _needs_merge_request(_output(success=True, test_already_exists=True)) is False
    assert (
        _needs_merge_request(_output(success=True, test_already_exists=True, adapted_existing=True)) is True
    )
    assert _needs_merge_request(_output(success=False)) is False
    assert _needs_merge_request(_output(success=True, lock_deferred=True)) is False


def test_resolve_test_dir_uses_agent_relative_path(tmp_path: Path):
    security = tmp_path / "Security" / "CVE-2026-11331"
    security.mkdir(parents=True)
    (security / "runtest.sh").write_text("#!/bin/bash\n")

    assert _resolve_test_dir(tmp_path, "Security/CVE-2026-11331") == security.resolve()
    assert _resolve_test_dir(tmp_path, "/Security/CVE-2026-11331") == security.resolve()


def test_resolve_test_dir_accepts_nonstandard_layout(tmp_path: Path):
    custom = tmp_path / "General" / "bind" / "RHEL-213761"
    custom.mkdir(parents=True)
    (custom / "main.fmf").write_text("summary: x\n")

    assert _resolve_test_dir(tmp_path, "General/bind/RHEL-213761") == custom.resolve()


def test_resolve_test_dir_rejects_traversal_and_missing(tmp_path: Path):
    assert _resolve_test_dir(tmp_path, None) is None
    assert _resolve_test_dir(tmp_path, "") is None
    assert _resolve_test_dir(tmp_path, "../etc") is None
    assert _resolve_test_dir(tmp_path, "Security/CVE-missing") is None


def test_reproducer_agent_enables_context_management():
    with (
        patch("ymir.agents.reproducer_agent.get_chat_model") as mock_get_model,
        patch("ymir.agents.reproducer_agent.is_reasoning_enabled", return_value=False),
        patch("ymir.agents.reproducer_agent.get_tool_call_checker_config"),
    ):
        llm = MagicMock()
        llm.allow_parallel_tool_calls = False
        mock_get_model.return_value = llm

        from ymir.agents.reproducer_agent import create_reproducer_agent

        agent = create_reproducer_agent(gateway_tools=[])
        assert agent._enable_context_management is True
        assert llm.allow_parallel_tool_calls is True


async def _git_init_with_main(repo: Path) -> None:
    await check_subprocess(["git", "init", "-b", "main"], cwd=repo)
    await check_subprocess(["git", "config", "user.email", "test@example.com"], cwd=repo)
    await check_subprocess(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "README").write_text("base\n")
    await check_subprocess(["git", "add", "README"], cwd=repo)
    await check_subprocess(["git", "commit", "-m", "init"], cwd=repo)


@pytest.mark.asyncio
async def test_prepare_reproducer_branch_new_mr_preserves_test_dir(tmp_path: Path):
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    await _git_init_with_main(repo)

    test_dir = repo / "Security" / "CVE-1"
    test_dir.mkdir(parents=True)
    (test_dir / "runtest.sh").write_text("adapted-on-default\n")

    branch = await _prepare_reproducer_branch(
        repo,
        test_dir,
        "reproducer/RHEL-1",
        adapted_existing=False,
        existing_mr_url=None,
        available_tools=[],
    )

    assert branch == "reproducer/RHEL-1"
    head, _ = await check_subprocess(["git", "branch", "--show-current"], cwd=repo)
    assert head.strip() == "reproducer/RHEL-1"
    assert (test_dir / "runtest.sh").read_text() == "adapted-on-default\n"


@pytest.mark.asyncio
async def test_prepare_reproducer_branch_adapt_keeps_sibling_commits(tmp_path: Path):
    """Adapt must land on the MR tip (sibling commit), not wipe it via checkout -B HEAD."""
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    await _git_init_with_main(repo)

    # Simulate an existing MR branch that already has a sibling commit.
    await check_subprocess(["git", "checkout", "-b", "reproducer/RHEL-1"], cwd=repo)
    mr_dir = repo / "Security" / "CVE-1"
    mr_dir.mkdir(parents=True)
    (mr_dir / "runtest.sh").write_text("sibling-stream\n")
    await check_subprocess(["git", "add", "Security"], cwd=repo)
    await check_subprocess(["git", "commit", "-m", "sibling adapt"], cwd=repo)
    sibling_sha, _ = await check_subprocess(["git", "rev-parse", "HEAD"], cwd=repo)

    # Agent continued on main with a local adaptation of the same test path.
    await check_subprocess(["git", "checkout", "main"], cwd=repo)
    test_dir = repo / "Security" / "CVE-1"
    test_dir.mkdir(parents=True)
    (test_dir / "runtest.sh").write_text("local-adapt\n")

    details = MergeRequestDetails(
        source_repo="https://gitlab.com/fork/tests-pkg.git",
        source_branch="reproducer/RHEL-1",
        target_repo_name="pkg",
        target_branch="main",
        title="adapt",
        description="",
        last_updated_at=datetime.now(UTC),
        comments=[],
    )

    async def fake_run_tool(name, available_tools=None, **kwargs):
        if name == "get_merge_request_details":
            return details.model_dump(mode="json")
        if name == "fetch_branch":
            # Local stand-in: branch already exists; nothing to fetch.
            return "ok"
        raise AssertionError(f"unexpected tool {name}")

    with patch("ymir.agents.reproducer_agent.run_tool", new=AsyncMock(side_effect=fake_run_tool)):
        branch = await _prepare_reproducer_branch(
            repo,
            test_dir,
            "reproducer/RHEL-1",
            adapted_existing=True,
            existing_mr_url="https://gitlab.com/redhat/rhel/tests/pkg/-/merge_requests/1",
            available_tools=[],
        )

    assert branch == "reproducer/RHEL-1"
    head, _ = await check_subprocess(["git", "branch", "--show-current"], cwd=repo)
    assert head.strip() == "reproducer/RHEL-1"
    # HEAD is still the sibling commit (not a reset of main).
    sha, _ = await check_subprocess(["git", "rev-parse", "HEAD"], cwd=repo)
    assert sha.strip() == sibling_sha.strip()
    # Local adaptations were restored on top of that tip.
    assert (test_dir / "runtest.sh").read_text() == "local-adapt\n"
