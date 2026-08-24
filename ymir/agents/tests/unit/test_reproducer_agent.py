"""Unit tests for reproducer agent label and comment helpers."""

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ymir.agents.reproducer_agent import (
    PreparedTestsClone,
    _bootstrap_tests_clone,
    _build_mr_title,
    _cve_only_needles,
    _determine_comment_resolution,
    _determine_result_label,
    _discover_existing_reproducer_test_dir,
    _match_open_reproducer_mr,
    _match_open_reproducer_mr_for_input,
    _match_regression_sibling_mr,
    _needs_merge_request,
    _prepare_reproducer_branch,
    _reproducer_mr_title_tags,
    _resolve_reproducer_mr_target,
    _resolve_test_dir,
    _reviewer_lookup_branch,
    _should_finalize_jira,
    create_reproducer_agent,
    main,
)
from ymir.agents.tasks import InvalidReproducerConfigError, fetch_reproducer_config
from ymir.common.base_utils import check_subprocess
from ymir.common.constants import JiraLabels
from ymir.common.models import MergeRequestDetails, ReproducerInputSchema, ReproducerOutputSchema, Task


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


@pytest.mark.parametrize(
    ("input_data", "expected"),
    [
        (ReproducerInputSchema(jira_issue="RHEL-1", package="bind", target_branch="c10s"), "c10s"),
        (
            ReproducerInputSchema(jira_issue="RHEL-1", package="bind", fix_version="rhel-9.8"),
            "rhel-9.8.0",
        ),
        (ReproducerInputSchema(jira_issue="RHEL-1", package="bind"), None),
    ],
)
def test_reviewer_lookup_branch(input_data, expected):
    assert _reviewer_lookup_branch(input_data) == expected


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


def test_discover_existing_reproducer_test_dir_prefers_security_cve(tmp_path: Path):
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    canonical = repo / "Security" / "CVE-2026-50219"
    canonical.mkdir(parents=True)
    (canonical / "main.fmf").write_text("summary: test\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id="CVE-2026-50219",
        jira_issue="RHEL-220981",
        reproducer_type="cve",
    )
    assert discovered == canonical


def test_discover_existing_reproducer_test_dir_rejects_cve_prefix_collision(tmp_path: Path):
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    for cve in ("CVE-2026-1", "CVE-2026-10"):
        test_dir = repo / "Security" / cve
        test_dir.mkdir(parents=True)
        (test_dir / "main.fmf").write_text(f"summary: {cve}\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id="CVE-2026-10",
        jira_issue="RHEL-1",
        reproducer_type="cve",
    )
    assert discovered == repo / "Security" / "CVE-2026-10"


def test_discover_existing_reproducer_test_dir_finds_sole_regression_dir(tmp_path: Path):
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    regression = repo / "Regression" / "RHEL-100"
    regression.mkdir(parents=True)
    (regression / "runtest.sh").write_text("#!/bin/bash\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id=None,
        jira_issue="RHEL-200",
        reproducer_type="bug",
    )
    assert discovered == regression


def test_cve_only_needles_splits_and_normalizes():
    assert _cve_only_needles("CVE-2026-56132") == ["CVE-2026-56132"]
    assert _cve_only_needles("cve-1; CVE-2") == ["CVE-1", "CVE-2"]
    assert _cve_only_needles(None) == []


def test_match_open_reproducer_mr_by_cve_bracket_in_title():
    mrs = [
        {
            "url": "https://gitlab.com/redhat/rhel/tests/expat/-/merge_requests/17",
            "title": "expat: [CVE-2026-56132] ymir reproducer test",
            "description": "Follow-up for CVE-2026-99999 mentioned here only.",
            "source_branch": "reproducer/RHEL-221017",
        }
    ]
    matched = _match_open_reproducer_mr(mrs, cve_ids=["CVE-2026-56132"])
    assert matched is not None
    assert matched["source_branch"] == "reproducer/RHEL-221017"


def test_match_open_reproducer_mr_ignores_unrelated_cve_in_description():
    mrs = [
        {
            "url": "https://gitlab.com/redhat/rhel/tests/expat/-/merge_requests/99",
            "title": "expat: [CVE-2026-56132] ymir reproducer test",
            "description": "Test for CVE-2026-99999 which is unrelated.",
        }
    ]
    assert _match_open_reproducer_mr(mrs, cve_ids=["CVE-2026-99999"]) is None
    assert _match_open_reproducer_mr(mrs, cve_ids=["CVE-2026-56132"]) is not None


def test_match_open_reproducer_mr_ignores_bare_cve_token_without_brackets():
    mrs = [
        {
            "url": "https://gitlab.com/redhat/rhel/tests/expat/-/merge_requests/17",
            "title": "expat: add cve reproducer for RHEL-221017 (CVE-2026-56132)",
            "description": "Security test for CVE-2026-56132 in expat.",
        }
    ]
    assert _match_open_reproducer_mr(mrs, cve_ids=["CVE-2026-56132"]) is None


def test_match_regression_sibling_mr_when_issue_not_yet_in_title():
    mrs = [
        {
            "url": "https://gitlab.com/a/1",
            "title": "bind: [RHEL-100] ymir reproducer test",
        },
        {
            "url": "https://gitlab.com/a/2",
            "title": "bind: [RHEL-500] ymir reproducer test",
        },
    ]
    assert _match_regression_sibling_mr(mrs, "RHEL-300") is None
    assert _match_regression_sibling_mr(mrs, "RHEL-300", clone_root="RHEL-100") == mrs[0]

    single = [mrs[0]]
    assert _match_regression_sibling_mr(single, "RHEL-200") == single[0]
    assert _match_regression_sibling_mr(single, "RHEL-200", clone_root="RHEL-100") == single[0]


def test_match_open_reproducer_mr_uses_clone_root_tag():
    mrs = [
        {
            "url": "https://gitlab.com/a/1",
            "title": "bind: [RHEL-100] ymir reproducer test",
        },
        {
            "url": "https://gitlab.com/a/2",
            "title": "bind: [RHEL-500] ymir reproducer test",
        },
    ]
    assert _match_open_reproducer_mr(mrs, jira_issue="RHEL-200", clone_root="RHEL-100") == mrs[0]
    assert _match_open_reproducer_mr(mrs, jira_issue="RHEL-600", clone_root="RHEL-500") == mrs[1]


def test_discover_existing_reproducer_test_dir_prefers_clone_root_regression_path(tmp_path: Path):
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    root_dir = repo / "Regression" / "RHEL-100"
    root_dir.mkdir(parents=True)
    (root_dir / "runtest.sh").write_text("#!/bin/bash\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id=None,
        jira_issue="RHEL-300",
        reproducer_type="bug",
        clone_root="RHEL-100",
    )
    assert discovered == root_dir


@pytest.mark.asyncio
async def test_match_open_reproducer_mr_for_input_uses_clone_root(monkeypatch):
    mrs = [
        {
            "url": "https://gitlab.com/a/1",
            "title": "bind: [RHEL-100] ymir reproducer test",
        },
        {
            "url": "https://gitlab.com/a/2",
            "title": "bind: [RHEL-500] ymir reproducer test",
        },
    ]
    monkeypatch.setattr(
        "ymir.agents.reproducer_agent._resolve_reproducer_clone_root",
        AsyncMock(return_value="RHEL-100"),
    )
    input_data = ReproducerInputSchema(jira_issue="RHEL-300", package="bind")
    matched = await _match_open_reproducer_mr_for_input(input_data, mrs)
    assert matched == mrs[0]


@pytest.mark.asyncio
async def test_resolve_reproducer_mr_target_extends_clone_chain_mr_with_multiple_open():
    result = _output(
        jira_issue="RHEL-300",
        success=True,
        test_directory="Regression/RHEL-100",
        package="bind",
        reproducer_type="bug",
    )
    agent_input = ReproducerInputSchema(jira_issue="RHEL-300", package="bind")
    open_mrs = [
        {
            "url": "https://gitlab.com/redhat/rhel/tests/bind/-/merge_requests/5",
            "title": "bind: [RHEL-100] ymir reproducer test",
            "source_branch": "reproducer/RHEL-100",
        },
        {
            "url": "https://gitlab.com/redhat/rhel/tests/bind/-/merge_requests/6",
            "title": "bind: [RHEL-500] ymir reproducer test",
            "source_branch": "reproducer/RHEL-500",
        },
    ]

    async def fake_run_tool(name, available_tools=None, **kwargs):
        if name == "list_project_merge_requests":
            return open_mrs
        raise AssertionError(name)

    with (
        patch("ymir.agents.reproducer_agent.run_tool", new=AsyncMock(side_effect=fake_run_tool)),
        patch(
            "ymir.agents.reproducer_agent._resolve_reproducer_clone_root",
            new=AsyncMock(return_value="RHEL-100"),
        ),
    ):
        mr_url, branch, matched_mr = await _resolve_reproducer_mr_target(result, agent_input, "bind", [])

    assert mr_url == open_mrs[0]["url"]
    assert branch == "reproducer/RHEL-100"
    assert (
        _build_mr_title(result, agent_input, matched_mr=matched_mr)
        == "bind: [RHEL-100, RHEL-300] ymir reproducer test"
    )


def test_build_mr_title_appends_jira_on_regression_adapt():
    matched_mr = {"title": "bind: [RHEL-100] ymir reproducer test"}
    result = _output(package="bind", reproducer_type="bug", jira_issue="RHEL-200")
    agent_input = ReproducerInputSchema(jira_issue="RHEL-200", package="bind")
    assert (
        _build_mr_title(result, agent_input, matched_mr=matched_mr)
        == "bind: [RHEL-100, RHEL-200] ymir reproducer test"
    )


def test_build_mr_title_keeps_cve_tag_when_updating_existing_mr():
    matched_mr = {"title": "expat: [CVE-2026-56132] ymir reproducer test"}
    result = _output(package="expat", reproducer_type="cve", jira_issue="RHEL-221014")
    agent_input = ReproducerInputSchema(
        jira_issue="RHEL-221014",
        package="expat",
        cve_id="CVE-2026-56132",
    )
    assert (
        _build_mr_title(result, agent_input, matched_mr=matched_mr)
        == "expat: [CVE-2026-56132] ymir reproducer test"
    )


def test_match_open_reproducer_mr_by_jira_bracket():
    mrs = [
        {
            "url": "https://gitlab.com/a/1",
            "title": "bind: [RHEL-99999] ymir reproducer test",
            "description": "Also mentions RHEL-88888 in prose.",
        }
    ]
    matched = _match_open_reproducer_mr(mrs, jira_issue="RHEL-99999")
    assert matched is not None
    assert _match_open_reproducer_mr(mrs, jira_issue="RHEL-88888") is None


def test_build_mr_title_uses_cve_bracket_for_security():
    result = _output(package="expat", reproducer_type="cve", jira_issue="RHEL-221014")
    agent_input = ReproducerInputSchema(
        jira_issue="RHEL-221014",
        package="expat",
        cve_id="CVE-2026-56132",
    )
    assert _build_mr_title(result, agent_input) == "expat: [CVE-2026-56132] ymir reproducer test"


def test_build_mr_title_multi_cve_roundtrip():
    """Multi-CVE titles must use separate bracket tags so the regex can parse each one."""
    result = _output(package="expat", reproducer_type="cve", jira_issue="RHEL-221014")
    agent_input = ReproducerInputSchema(
        jira_issue="RHEL-221014",
        package="expat",
        cve_id="CVE-2026-56132, CVE-2026-50219",
    )
    title = _build_mr_title(result, agent_input)
    assert title == "expat: [CVE-2026-50219] [CVE-2026-56132] ymir reproducer test"

    cves, jiras = _reproducer_mr_title_tags(title)
    assert cves == {"CVE-2026-50219", "CVE-2026-56132"}
    assert jiras == set()

    matched = _match_open_reproducer_mr(
        [{"url": "https://gitlab.com/a/1", "title": title}],
        cve_ids=["CVE-2026-56132"],
    )
    assert matched is not None

    matched2 = _match_open_reproducer_mr(
        [{"url": "https://gitlab.com/a/1", "title": title}],
        cve_ids=["CVE-2026-50219"],
    )
    assert matched2 is not None


def test_build_mr_title_uses_jira_bracket_for_regression():
    result = _output(package="bind", reproducer_type="bug", jira_issue="RHEL-12345")
    agent_input = ReproducerInputSchema(jira_issue="RHEL-12345", package="bind")
    assert _build_mr_title(result, agent_input) == "bind: [RHEL-12345] ymir reproducer test"


def test_match_open_reproducer_mr_prefers_existing_url():
    mrs = [
        {"url": "https://gitlab.com/a/1", "title": "other", "description": ""},
        {"url": "https://gitlab.com/a/2", "title": "target", "description": ""},
    ]
    matched = _match_open_reproducer_mr(
        mrs,
        cve_ids=["CVE-1"],
        existing_mr_url="https://gitlab.com/a/2",
    )
    assert matched["url"] == "https://gitlab.com/a/2"


@pytest.mark.asyncio
async def test_resolve_reproducer_mr_target_uses_open_cve_mr():
    result = _output(
        jira_issue="RHEL-221014",
        success=True,
        test_directory="Security/CVE-2026-56132",
        package="expat",
        reproducer_type="cve",
    )
    agent_input = ReproducerInputSchema(
        jira_issue="RHEL-221014",
        package="expat",
        cve_id="CVE-2026-56132",
    )
    open_mrs = [
        {
            "url": "https://gitlab.com/redhat/rhel/tests/expat/-/merge_requests/17",
            "title": "expat: [CVE-2026-56132] ymir reproducer test",
            "description": "Security test for CVE-2026-56132 in expat.",
            "source_branch": "reproducer/RHEL-221017",
        }
    ]

    async def fake_run_tool(name, available_tools=None, **kwargs):
        if name == "list_project_merge_requests":
            return open_mrs
        raise AssertionError(name)

    with patch("ymir.agents.reproducer_agent.run_tool", new=AsyncMock(side_effect=fake_run_tool)):
        mr_url, branch, matched_mr = await _resolve_reproducer_mr_target(result, agent_input, "expat", [])

    assert mr_url == open_mrs[0]["url"]
    assert branch == "reproducer/RHEL-221017"
    assert matched_mr == open_mrs[0]
    assert result.adapted_existing is True
    assert result.existing_mr_url == open_mrs[0]["url"]


@pytest.mark.asyncio
async def test_resolve_reproducer_mr_target_extends_regression_mr_for_sibling():
    result = _output(
        jira_issue="RHEL-200",
        success=True,
        test_directory="Regression/RHEL-200",
        package="bind",
        reproducer_type="bug",
    )
    agent_input = ReproducerInputSchema(jira_issue="RHEL-200", package="bind")
    open_mrs = [
        {
            "url": "https://gitlab.com/redhat/rhel/tests/bind/-/merge_requests/5",
            "title": "bind: [RHEL-100] ymir reproducer test",
            "source_branch": "reproducer/RHEL-100",
        }
    ]

    async def fake_run_tool(name, available_tools=None, **kwargs):
        if name == "list_project_merge_requests":
            return open_mrs
        raise AssertionError(name)

    with patch("ymir.agents.reproducer_agent.run_tool", new=AsyncMock(side_effect=fake_run_tool)):
        mr_url, branch, matched_mr = await _resolve_reproducer_mr_target(result, agent_input, "bind", [])

    assert mr_url == open_mrs[0]["url"]
    assert branch == "reproducer/RHEL-100"
    assert matched_mr == open_mrs[0]
    assert result.adapted_existing is True
    assert (
        _build_mr_title(result, agent_input, matched_mr=matched_mr)
        == "bind: [RHEL-100, RHEL-200] ymir reproducer test"
    )


@pytest.mark.asyncio
async def test_resolve_reproducer_mr_target_new_branch_without_open_mr():
    result = _output(jira_issue="RHEL-221017", success=True, package="expat", reproducer_type="cve")
    agent_input = ReproducerInputSchema(
        jira_issue="RHEL-221017",
        package="expat",
        cve_id="CVE-2026-56132",
    )

    async def fake_run_tool(name, available_tools=None, **kwargs):
        if name == "list_project_merge_requests":
            return []
        raise AssertionError(name)

    with patch("ymir.agents.reproducer_agent.run_tool", new=AsyncMock(side_effect=fake_run_tool)):
        mr_url, branch, matched_mr = await _resolve_reproducer_mr_target(result, agent_input, "expat", [])

    assert mr_url is None
    assert branch == "reproducer/RHEL-221017"
    assert matched_mr is None
    assert result.adapted_existing is False


def test_reproducer_agent_enables_context_management():
    with (
        patch("ymir.agents.reproducer_agent.get_chat_model") as mock_get_model,
        patch("ymir.agents.reproducer_agent.is_reasoning_enabled", return_value=False),
        patch("ymir.agents.reproducer_agent.get_tool_call_checker_config"),
    ):
        llm = MagicMock()
        llm.allow_parallel_tool_calls = False
        mock_get_model.return_value = llm

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

    branch, commit_dir = await _prepare_reproducer_branch(
        repo,
        test_dir,
        "reproducer/RHEL-1",
        existing_mr_url=None,
        available_tools=[],
    )

    assert branch == "reproducer/RHEL-1"
    assert commit_dir == test_dir
    head, _ = await check_subprocess(["git", "branch", "--show-current"], cwd=repo)
    assert head.strip() == "reproducer/RHEL-1"
    assert (test_dir / "runtest.sh").read_text() == "adapted-on-default\n"


@pytest.mark.asyncio
async def test_prepare_reproducer_branch_bootstrapped_adapt_preserves_worktree(tmp_path: Path):
    """Bootstrapped adapt must not re-overlay — agent edits are already on the MR branch."""
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    await _git_init_with_main(repo)

    await check_subprocess(["git", "checkout", "-b", "reproducer/RHEL-1"], cwd=repo)
    test_dir = repo / "Security" / "CVE-1"
    test_dir.mkdir(parents=True)
    (test_dir / "main.fmf").write_text("summary: sibling\n")
    (test_dir / "runtest.sh").write_text("local-adapt\n")
    await check_subprocess(["git", "add", "Security"], cwd=repo)
    await check_subprocess(["git", "commit", "-m", "sibling adapt"], cwd=repo)
    sibling_sha, _ = await check_subprocess(["git", "rev-parse", "HEAD"], cwd=repo)

    bootstrap = PreparedTestsClone(
        tests_clone=repo,
        existing_mr_url="https://gitlab.com/redhat/rhel/tests/pkg/-/merge_requests/1",
        mr_source_branch="reproducer/RHEL-1",
        existing_test_directory="Security/CVE-1",
        matched_mr={"url": "https://gitlab.com/redhat/rhel/tests/pkg/-/merge_requests/1"},
    )

    branch, commit_dir = await _prepare_reproducer_branch(
        repo,
        test_dir,
        "reproducer/RHEL-1",
        existing_mr_url=bootstrap.existing_mr_url,
        available_tools=[],
        bootstrap=bootstrap,
    )

    assert branch == "reproducer/RHEL-1"
    assert commit_dir == test_dir
    sha, _ = await check_subprocess(["git", "rev-parse", "HEAD"], cwd=repo)
    assert sha.strip() == sibling_sha.strip()
    assert (test_dir / "runtest.sh").read_text() == "local-adapt\n"


@pytest.mark.asyncio
async def test_bootstrap_tests_clone_checks_out_existing_mr_branch(tmp_path: Path, monkeypatch):
    working_dir = tmp_path / "Reproducer" / "RHEL-2"
    working_dir.mkdir(parents=True)
    repo = working_dir / "tests-bind"
    repo.mkdir()
    cve_id = "CVE-2026-0001"

    async def fake_run_tool(name, available_tools=None, **kwargs):
        if name == "clone_repository":
            (repo / "README").write_text("cloned\n")
            return "ok"
        if name == "list_project_merge_requests":
            return [
                {
                    "url": "https://gitlab.com/redhat/rhel/tests/bind/-/merge_requests/9",
                    "title": f"bind: [{cve_id}] ymir reproducer test",
                    "source_branch": "reproducer/RHEL-1",
                }
            ]
        if name == "get_merge_request_details":
            return MergeRequestDetails(
                source_repo="https://gitlab.com/fork/tests-bind.git",
                source_branch="reproducer/RHEL-1",
                target_repo_name="bind",
                target_branch="main",
                title=f"bind: [{cve_id}] ymir reproducer test",
                description="",
                last_updated_at=datetime.now(UTC),
                comments=[],
            ).model_dump(mode="json")
        if name == "fetch_branch":
            await _git_init_with_main(repo)
            await check_subprocess(["git", "checkout", "-b", "reproducer/RHEL-1"], cwd=repo)
            mr_dir = repo / "Security" / cve_id
            mr_dir.mkdir(parents=True)
            (mr_dir / "main.fmf").write_text("summary: on mr\n")
            await check_subprocess(["git", "add", "Security"], cwd=repo)
            await check_subprocess(["git", "commit", "-m", "mr test"], cwd=repo)
            return "ok"
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr("ymir.agents.reproducer_agent.run_tool", fake_run_tool)

    input_data = ReproducerInputSchema(
        jira_issue="RHEL-2",
        package="bind",
        cve_id=cve_id,
    )
    bootstrap = await _bootstrap_tests_clone(working_dir, input_data, [])

    assert bootstrap.existing_mr_url.endswith("/merge_requests/9")
    assert bootstrap.mr_source_branch == "reproducer/RHEL-1"
    assert bootstrap.existing_test_directory == f"Security/{cve_id}"
    head, _ = await check_subprocess(["git", "branch", "--show-current"], cwd=repo)
    assert head.strip() == "reproducer/RHEL-1"


# =============================================================================
# process_task tests
# =============================================================================


def _make_reproducer_payload(issue: str = "RHEL-99999", user_triggered: bool = False) -> bytes:
    input_data = ReproducerInputSchema(jira_issue=issue, package="bind")
    task = Task(metadata=input_data.model_dump(), user_triggered=user_triggered)
    return task.model_dump_json().encode()


@contextlib.contextmanager
def _mock_reproducer_config_enabled():
    enabled_config = MagicMock(enabled=True)

    @contextlib.asynccontextmanager
    async def fake_mcp_tools(*_args, **_kwargs):
        yield []

    with (
        patch(
            "ymir.agents.tasks.fetch_reproducer_config", new_callable=AsyncMock, return_value=enabled_config
        ),
        patch("ymir.agents.reproducer_agent.mcp_tools", side_effect=fake_mcp_tools),
    ):
        yield


@contextlib.contextmanager
def _mock_workflow_lock():
    with (
        patch(
            "ymir.agents.reproducer_agent.resolve_reproducer_lock_id",
            new_callable=AsyncMock,
            return_value="RHEL-99999",
        ),
        patch(
            "ymir.agents.reproducer_agent.try_acquire_reproducer_lock",
            new_callable=AsyncMock,
            return_value='{"package":"bind","lock_id":"RHEL-99999","jira_issue":"RHEL-99999"}',
        ),
        patch(
            "ymir.agents.reproducer_agent.release_reproducer_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        yield


async def _run_process_task(payload: bytes) -> None:
    """Run reproducer main() in queue mode, invoking process_task with payload once.

    process_task is a closure defined inside main() and cannot be imported directly.
    This helper runs main() with a fake run_task_loop that calls process_fn(payload)
    immediately, so process_task executes inside main()'s redis context — matching
    the production execution environment.  Test-specific patches (e.g. get_jira_issue_metadata,
    run_workflow) must be applied by the caller before invoking this helper.
    """
    span_processor = MagicMock()
    span_processor.start_transaction.return_value.__enter__ = MagicMock(return_value=None)
    span_processor.start_transaction.return_value.__exit__ = MagicMock(return_value=False)

    async def fake_run_task_loop(_redis, _queues, process_fn, **_kw):
        await process_fn(payload)

    with (
        patch("ymir.agents.reproducer_agent.init_sentry"),
        patch("ymir.agents.reproducer_agent.configure_logging"),
        patch("ymir.agents.reproducer_agent.resolve_chat_model_override"),
        patch("ymir.agents.reproducer_agent.setup_observability", return_value=span_processor),
        patch("ymir.agents.reproducer_agent.run_task_loop", side_effect=fake_run_task_loop),
        patch("ymir.agents.reproducer_agent.redis_client") as mock_redis_ctx,
        patch.dict(
            "os.environ",
            {"COLLECTOR_ENDPOINT": "http://localhost:4317", "REDIS_URL": "redis://localhost"},
            clear=False,
        ),
    ):
        mock_redis_ctx.return_value.__aenter__ = AsyncMock()
        mock_redis_ctx.return_value.__aexit__ = AsyncMock()
        await main()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_label",
    [
        "ymir_reproducer_created",
        "ymir_reproducer_failed",
        "ymir_reproducer_errored",
        "ymir_reproducer_not_reproducible",
        "ymir_reproducer_already_exists",
    ],
)
async def test_process_task_skips_duplicate_with_terminal_label(terminal_label):
    """When a terminal label is already set and the task is not user-triggered,
    process_task must skip without calling run_workflow.

    This is a regression guard for the get_jira_labels → get_jira_issue_metadata
    rename: the function must be called and its tuple return value unpacked correctly.
    """
    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([terminal_label], "New"),
        ) as mock_get_metadata,
        patch("ymir.agents.reproducer_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
    ):
        await _run_process_task(_make_reproducer_payload())

    mock_get_metadata.assert_awaited_once_with("RHEL-99999")
    mock_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_task_proceeds_despite_terminal_label_when_user_triggered():
    """A user-triggered run must always proceed even when a terminal label is set."""
    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=(["ymir_reproducer_created"], "New"),
        ),
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock),
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock),
        patch("ymir.agents.reproducer_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
        _mock_reproducer_config_enabled(),
        _mock_workflow_lock(),
    ):
        mock_workflow.return_value = MagicMock(
            result=MagicMock(success=True, retryable_error=False, lock_deferred=False, summary="ok")
        )
        await _run_process_task(_make_reproducer_payload(user_triggered=True))

    mock_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_task_proceeds_when_terminal_label_and_in_progress():
    """If the in-progress label is set alongside a terminal label, the task
    must still be processed — the in-progress label signals an active run."""
    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=(["ymir_reproducer_created", "ymir_reproducer_in_progress"], "New"),
        ),
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock),
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock),
        patch("ymir.agents.reproducer_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
        _mock_reproducer_config_enabled(),
        _mock_workflow_lock(),
    ):
        mock_workflow.return_value = MagicMock(
            result=MagicMock(success=True, retryable_error=False, lock_deferred=False, summary="ok")
        )
        await _run_process_task(_make_reproducer_payload())

    mock_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_task_proceeds_when_no_terminal_labels():
    """An issue with no terminal labels goes through the full workflow."""
    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([], "New"),
        ),
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock),
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock),
        patch("ymir.agents.reproducer_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
        _mock_reproducer_config_enabled(),
        _mock_workflow_lock(),
    ):
        mock_workflow.return_value = MagicMock(
            result=MagicMock(success=True, retryable_error=False, lock_deferred=False, summary="ok")
        )
        await _run_process_task(_make_reproducer_payload())

    mock_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_task_blocks_when_workflow_lock_busy():
    """Busy create/adapt locks park the task until the holder releases."""
    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([], "New"),
        ),
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock),
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock),
        patch("ymir.agents.reproducer_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
        patch(
            "ymir.agents.reproducer_agent.resolve_reproducer_lock_id",
            new_callable=AsyncMock,
            return_value="CVE-2026-56132",
        ),
        patch(
            "ymir.agents.reproducer_agent.try_acquire_reproducer_lock",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "ymir.agents.reproducer_agent.enqueue_blocked_reproducer_task",
            new_callable=AsyncMock,
        ) as mock_enqueue_blocked,
        _mock_reproducer_config_enabled(),
    ):
        await _run_process_task(_make_reproducer_payload())

    mock_workflow.assert_not_awaited()
    mock_enqueue_blocked.assert_awaited_once()


# -- fetch_reproducer_config ---------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_reproducer_config_returns_default_when_not_found():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "No maintainer rules found for package 'bind' (file 'ymir.yaml' not found)"
        config = await fetch_reproducer_config("bind", [])

    assert config.enabled is False


@pytest.mark.asyncio
async def test_fetch_reproducer_config_parses_enabled():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "reproducer:\n  enabled: true\n"
        config = await fetch_reproducer_config("bind", [])

    assert config.enabled is True


@pytest.mark.asyncio
async def test_fetch_reproducer_config_parses_disabled():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "reproducer:\n  enabled: false\n"
        config = await fetch_reproducer_config("bind", [])

    assert config.enabled is False


@pytest.mark.asyncio
async def test_fetch_reproducer_config_raises_on_malformed_section():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "reproducer:\n  enabled: not_a_bool\n"
        with pytest.raises(InvalidReproducerConfigError, match="malformed"):
            await fetch_reproducer_config("bind", [])


@pytest.mark.asyncio
async def test_process_task_skips_when_reproducer_disabled():
    disabled_config = MagicMock(enabled=False)

    @contextlib.asynccontextmanager
    async def fake_mcp_tools(*_args, **_kwargs):
        yield []

    with (
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([], "New"),
        ),
        patch(
            "ymir.agents.tasks.fetch_reproducer_config", new_callable=AsyncMock, return_value=disabled_config
        ),
        patch("ymir.agents.reproducer_agent.mcp_tools", side_effect=fake_mcp_tools),
        patch("ymir.agents.reproducer_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
        patch(
            "ymir.agents.reproducer_agent.try_acquire_reproducer_lock",
            new_callable=AsyncMock,
        ) as mock_acquire_lock,
    ):
        await _run_process_task(_make_reproducer_payload())

    mock_workflow.assert_not_awaited()
    mock_acquire_lock.assert_not_awaited()
