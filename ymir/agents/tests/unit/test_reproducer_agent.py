"""Unit tests for reproducer agent label and comment helpers."""

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flexmock import flexmock

from ymir.agents import reproducer_agent as r_agent
from ymir.agents import tasks as agent_tasks
from ymir.agents.reproducer_agent import (
    PreparedTestsClone,
    _bootstrap_tests_clone,
    _build_mr_title,
    _clear_finalized_reproducer_errors,
    _cve_only_needles,
    _determine_comment_resolution,
    _determine_result_label,
    _discover_existing_reproducer_test_dir,
    _match_open_reproducer_mr,
    _match_open_reproducer_mr_for_input,
    _match_regression_sibling_mr,
    _needs_merge_request,
    _prepare_reproducer_branch,
    _render_fmf_templates,
    _render_prompt,
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
from ymir.common.constants import GITLAB_MR_PAGE_SIZE, JiraLabels, RedisQueues
from ymir.common.models import (
    ErrorData,
    ErrorListEntry,
    MergeRequestDetails,
    PackageReproducerConfig,
    ReproducerInputSchema,
    ReproducerOutputSchema,
    Task,
)


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


@pytest.mark.parametrize("result_issue", ["RHEL-12345", "RHEL-99999"])
def test_reproducer_description_links_to_input_issue_traces(monkeypatch, result_issue):
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example")
    result = _output(jira_issue=result_issue)
    input_data = ReproducerInputSchema(jira_issue="RHEL-12345", package=result.package)

    description = r_agent._build_mr_description(result, input_data)

    assert "[View all traces for RHEL-12345](https://trace.example/#/issues/RHEL-12345)" in description
    assert "https://trace.example/#/issues/RHEL-99999" not in description


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_overrides", "dry_run", "expected_removed"),
    [
        ({"success": False, "not_reproducible_reason": "race"}, False, 1),
        ({"success": False, "test_already_exists": True}, False, 1),
        pytest.param(
            {
                "success": False,
                "test_already_exists": True,
                "adapted_existing": True,
                "summary": "MR creation failed",
            },
            False,
            0,
            id="adapted-existing-mr-update-failed",
        ),
        ({"success": True}, False, 1),
        ({"success": False}, False, 0),
        ({"success": False, "retryable_error": True, "not_reproducible_reason": "race"}, False, 0),
        ({"success": False, "lock_deferred": True, "test_already_exists": True}, False, 0),
        ({"success": False, "not_reproducible_reason": "race"}, True, 0),
    ],
)
async def test_finalized_reproducer_clears_only_resolved_errors(result_overrides, dry_run, expected_removed):
    input_data = ReproducerInputSchema(jira_issue="RHEL-12345", package="libfoo", target_branch="c10s")
    old_error = (
        ErrorListEntry(
            error_id=1,
            queue=RedisQueues.REPRODUCER_QUEUE.value,
            task=Task(metadata=input_data.model_dump()),
            error=ErrorData(jira_issue=input_data.jira_issue, details="prior failure"),
        )
        .model_dump_json()
        .encode()
    )

    class ErrorListRedis:
        def __init__(self):
            self.entries = [old_error]
            self.lrem_calls = 0

        async def lrange(self, *_args):
            return list(self.entries)

        async def lrem(self, _key, _count, raw):
            self.lrem_calls += 1
            self.entries.remove(raw)
            return 1

    redis = ErrorListRedis()
    await _clear_finalized_reproducer_errors(redis, input_data, _output(**result_overrides), dry_run=dry_run)

    assert len(redis.entries) == 1 - expected_removed
    assert redis.lrem_calls == expected_removed


def test_needs_merge_request():
    assert _needs_merge_request(_output(success=True)) is True
    assert _needs_merge_request(_output(success=True, test_already_exists=True)) is False
    assert (
        _needs_merge_request(_output(success=True, test_already_exists=True, adapted_existing=True)) is True
    )
    assert _needs_merge_request(_output(success=False)) is False
    assert _needs_merge_request(_output(success=True, lock_deferred=True)) is False


@pytest.mark.parametrize(
    ("repository_url", "expected"),
    [
        ("https://gitlab.com/redhat/rhel/tests/shared", True),
        ("https://gitlab.cee.redhat.com/redhat/rhel/tests/shared", False),
        ("https://example.com/redhat/rhel/tests/shared", False),
    ],
)
def test_is_gitlab_repository_matches_supported_host(repository_url, expected):
    assert r_agent._is_gitlab_repository(repository_url) is expected


@pytest.mark.asyncio
async def test_list_open_reproducer_mrs_skips_unsupported_host():
    flexmock(r_agent).should_receive("run_tool").never()

    mrs = await r_agent._list_open_reproducer_mrs(
        "https://gitlab.cee.redhat.com/redhat/rhel/tests/shared",
        available_tools=[],
    )

    assert mrs == []


@pytest.mark.asyncio
async def test_list_open_reproducer_mrs_fetches_all_pages():
    page_one = [
        {"iid": index, "title": f"pkg: [RHEL-{index}] ymir reproducer test"}
        for index in range(GITLAB_MR_PAGE_SIZE)
    ]
    page_two = [
        {"iid": GITLAB_MR_PAGE_SIZE, "title": f"pkg: [RHEL-{GITLAB_MR_PAGE_SIZE}] ymir reproducer test"}
    ]
    page_calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        assert name == "list_project_merge_requests"
        assert kwargs["project"] == "redhat/rhel/tests/shared"
        assert kwargs["state"] == "opened"
        assert kwargs["labels"] == ["ymir_reproducer"]
        page_calls.append(kwargs["page"])
        return {1: page_one, 2: page_two}[kwargs["page"]]

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    mrs = await r_agent._list_open_reproducer_mrs(
        "https://gitlab.com/redhat/rhel/tests/shared",
        available_tools=[],
    )

    assert len(mrs) == GITLAB_MR_PAGE_SIZE + 1
    assert mrs[-1] == page_two[0]
    assert (
        r_agent._match_open_reproducer_mr(
            mrs,
            package="pkg",
            jira_issue=f"RHEL-{GITLAB_MR_PAGE_SIZE}",
        )
        == page_two[0]
    )
    assert page_calls == [1, 2]


@pytest.mark.asyncio
async def test_list_open_reproducer_mrs_stops_when_page_repeats():
    repeated_page = [
        {"iid": index, "title": f"pkg: [RHEL-{index}] ymir reproducer test"}
        for index in range(GITLAB_MR_PAGE_SIZE)
    ]
    page_calls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        assert name == "list_project_merge_requests"
        page_calls.append(kwargs["page"])
        return repeated_page

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    with pytest.raises(r_agent.IncompleteReproducerMRListingError) as exc_info:
        await r_agent._list_open_reproducer_mrs(
            "https://gitlab.com/redhat/rhel/tests/shared",
            available_tools=[],
        )

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "pagination did not advance" in str(exc_info.value.__cause__)
    assert page_calls == [1, 2]


@pytest.mark.asyncio
async def test_list_open_reproducer_mrs_raises_on_later_page_failure():
    pages = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        assert name == "list_project_merge_requests"
        page = kwargs["page"]
        pages.append(page)
        if page == 1:
            return [{"iid": index} for index in range(GITLAB_MR_PAGE_SIZE)]
        raise RuntimeError("GitLab page two unavailable")

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    with pytest.raises(r_agent.IncompleteReproducerMRListingError, match="complete open-MR listing"):
        await r_agent._list_open_reproducer_mrs(
            "https://gitlab.com/redhat/rhel/tests/shared",
            available_tools=[],
        )

    assert pages == [1, 2]


@pytest.mark.asyncio
async def test_list_open_reproducer_mrs_raises_on_malformed_page():
    async def _mock_run_tool(name, available_tools=None, **_kwargs):
        assert name == "list_project_merge_requests"
        return {"unexpected": "response"}

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    with pytest.raises(r_agent.IncompleteReproducerMRListingError, match="complete open-MR listing"):
        await r_agent._list_open_reproducer_mrs(
            "https://gitlab.com/redhat/rhel/tests/shared",
            available_tools=[],
        )


def test_tmt_lint_fix_output_schema_rejects_workflow_fields():
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        r_agent.TmtLintFixOutputSchema.model_validate(
            {
                "success": True,
                "summary": "Fixed the FMF metadata.",
                "test_directory": "Regression/RHEL-OTHER",
            }
        )


def test_tmt_lint_fix_merge_preserves_original_workflow_fields():
    original = _output(
        jira_issue="RHEL-114607",
        reproducer_type="cve",
        package="expat",
        summary="Reproducer detects the buffer overflow in the crafted input.",
        compose="RHEL-9.8.0",
        arch="x86_64",
        test_directory="packages/expat/Security/CVE-2025-59375",
        test_already_exists=False,
        adapted_existing=False,
        testing_farm_request_id="tf-original",
    )
    fixed = r_agent.TmtLintFixOutputSchema(
        success=False,
        summary="Fixed the FMF metadata.",
        retryable_error=True,
    )

    result = r_agent._merge_tmt_lint_fix_result(original, fixed)

    assert result.jira_issue == "RHEL-114607"
    assert result.reproducer_type == "cve"
    assert result.package == "expat"
    assert result.compose == "RHEL-9.8.0"
    assert result.arch == "x86_64"
    assert result.test_directory == "packages/expat/Security/CVE-2025-59375"
    assert result.test_already_exists is False
    assert result.adapted_existing is False
    assert result.existing_mr_url is None
    assert result.testing_farm_request_id == "tf-original"
    assert result.success is False
    assert result.summary == (
        "Reproducer detects the buffer overflow in the crafted input.\n\nLint fix: Fixed the FMF metadata."
    )
    assert result.retryable_error is True

    with_new_tf_request = r_agent.TmtLintFixOutputSchema(
        success=True,
        summary="Verified after the fix.",
        testing_farm_request_id="tf-new",
    )
    result_with_new_tf_request = r_agent._merge_tmt_lint_fix_result(result, with_new_tf_request)
    assert result_with_new_tf_request.testing_farm_request_id == "tf-new"
    assert result_with_new_tf_request.summary == (
        "Reproducer detects the buffer overflow in the crafted input.\n\n"
        "Lint fix: Fixed the FMF metadata.\n\nLint fix: Verified after the fix."
    )


def test_tmt_lint_fix_merge_does_not_repeat_existing_lint_note():
    original = _output(summary="Original reproducer summary.\n\nLint fix: Added the missing require.")
    fixed = r_agent.TmtLintFixOutputSchema(
        success=True,
        summary="Added the missing require.",
    )

    result = r_agent._merge_tmt_lint_fix_result(original, fixed)

    assert result.summary == original.summary


def test_tmt_lint_fix_merge_keeps_original_summary_when_fix_summary_is_empty():
    original = _output(summary="Original reproducer summary.")
    fixed = r_agent.TmtLintFixOutputSchema(success=True, summary="  ")

    result = r_agent._merge_tmt_lint_fix_result(original, fixed)

    assert result.summary == original.summary


@pytest.mark.asyncio
async def test_resolve_reproducer_setup_loads_config_from_jira_component():
    config = PackageReproducerConfig(
        enabled=True,
        test_repository="https://gitlab.com/redhat/rhel/tests/shared-tests",
        test_directory_prefix="packages/bind",
    )

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        assert name == "get_jira_details"
        assert kwargs == {"issue_key": "RHEL-12345"}
        return {"fields": {"components": [{"name": "bind"}]}}

    async def _mock_fetch_reproducer_config(package, available_tools):
        assert package == "bind"
        return config

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(agent_tasks).should_receive("fetch_reproducer_config").replace_with(
        _mock_fetch_reproducer_config
    )

    resolved_input, resolved_config, repository_url = await r_agent._resolve_reproducer_setup(
        ReproducerInputSchema(jira_issue="RHEL-12345"),
        reproducer_config=None,
        available_tools=[],
    )

    assert resolved_input.package == "bind"
    assert resolved_config is config
    assert repository_url == config.test_repository


@pytest.mark.parametrize("components", [[], [{"name": "bind"}, {"name": "kernel"}]])
def test_package_from_jira_details_requires_exactly_one_component(components):
    with pytest.raises(ValueError, match="expected exactly one Jira component"):
        r_agent._package_from_jira_details({"fields": {"components": components}})


@pytest.mark.parametrize("package", ["bind", "python3.11", "gcc-c++", "libstdc++"])
def test_validate_reproducer_package_name_accepts_rpm_names(package):
    assert r_agent._validate_reproducer_package_name(package) == package


@pytest.mark.parametrize("package", ["", ".", "..", "../victim", "foo/bar", r"foo\bar", "bad package"])
def test_validate_reproducer_package_name_rejects_unsafe_names(package):
    with pytest.raises(ValueError, match="Invalid reproducer package name"):
        r_agent._validate_reproducer_package_name(package)


def test_validate_reproducer_output_package_requires_resolved_package():
    result = _output(package="other-package")

    with pytest.raises(ValueError, match="expected resolved package 'bind'"):
        r_agent._validate_reproducer_output_package(result, "bind")


def test_package_from_jira_details_rejects_path_like_component():
    with pytest.raises(ValueError, match="Invalid reproducer package name"):
        r_agent._package_from_jira_details({"fields": {"components": [{"name": "../victim"}]}})


@pytest.mark.parametrize("components", [[], [{"name": "bind"}, {"name": "kernel"}]])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.asyncio
async def test_standalone_package_resolution_error_stops_before_agent(
    monkeypatch,
    tmp_path: Path,
    components,
    dry_run,
):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    comments = []

    @contextlib.asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        assert name == "get_jira_details"
        assert kwargs["issue_key"] == "RHEL-12345"
        return {"fields": {"components": components}}

    async def _capture_terminal_error_comment(**kwargs):
        comments.append(kwargs)

    monkeypatch.setattr(r_agent, "mcp_tools", _mock_mcp_tools)
    monkeypatch.setattr(agent_tasks, "post_terminal_error_comment", _capture_terminal_error_comment)
    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    def _unexpected_agent_factory(*_args, **_kwargs):
        raise AssertionError("reproducer agent must not run without one package")

    state = await r_agent.run_workflow(
        "RHEL-12345",
        dry_run,
        _unexpected_agent_factory,
    )

    assert state.result is None
    assert not (tmp_path / "Reproducer" / "RHEL-12345" / "tests-bind").exists()
    if dry_run:
        assert comments == []
    else:
        assert len(comments) == 1
        assert comments[0]["jira_issue"] == "RHEL-12345"
        assert "exactly one Jira component" in comments[0]["comment_text"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.asyncio
async def test_standalone_invalid_reproducer_config_stops_before_agent(monkeypatch, tmp_path: Path, dry_run):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    comments = []

    @contextlib.asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        assert name == "get_jira_details"
        assert kwargs["issue_key"] == "RHEL-12345"
        return {"fields": {"components": [{"name": "bind"}]}}

    async def _invalid_config(*_args, **_kwargs):
        raise InvalidReproducerConfigError("ymir.yaml reproducer section for bind is malformed")

    async def _capture_terminal_error_comment(**kwargs):
        comments.append(kwargs)

    monkeypatch.setattr(r_agent, "mcp_tools", _mock_mcp_tools)
    monkeypatch.setattr(agent_tasks, "fetch_reproducer_config", _invalid_config)
    monkeypatch.setattr(agent_tasks, "post_terminal_error_comment", _capture_terminal_error_comment)
    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    def _unexpected_agent_factory(*_args, **_kwargs):
        raise AssertionError("reproducer agent must not run with invalid package configuration")

    state = await r_agent.run_workflow(
        "RHEL-12345",
        dry_run,
        _unexpected_agent_factory,
    )

    assert state.result is None
    assert not (tmp_path / "Reproducer" / "RHEL-12345" / "tests-bind").exists()
    if dry_run:
        assert comments == []
    else:
        assert len(comments) == 1
        assert comments[0]["jira_issue"] == "RHEL-12345"
        assert "reproducer section for bind is malformed" in comments[0]["comment_text"]
        assert "package rules repository" in comments[0]["comment_text"]
        assert "None" not in comments[0]["comment_text"]


@pytest.mark.asyncio
async def test_resolve_reproducer_setup_keeps_explicit_package_and_config():
    config = PackageReproducerConfig(
        enabled=True,
        test_repository="https://gitlab.com/redhat/rhel/tests/shared-tests",
    )
    flexmock(r_agent).should_receive("run_tool").never()
    flexmock(agent_tasks).should_receive("fetch_reproducer_config").never()

    resolved_input, resolved_config, repository_url = await r_agent._resolve_reproducer_setup(
        ReproducerInputSchema(jira_issue="RHEL-12345", package="openssl"),
        reproducer_config=config,
        available_tools=[],
    )

    assert resolved_input.package == "openssl"
    assert resolved_config is config
    assert repository_url == config.test_repository


@pytest.mark.asyncio
async def test_resolve_reproducer_setup_rejects_unsafe_supplied_package():
    flexmock(r_agent).should_receive("run_tool").never()
    flexmock(agent_tasks).should_receive("fetch_reproducer_config").never()

    with pytest.raises(ValueError, match="Invalid reproducer package name"):
        await r_agent._resolve_reproducer_setup(
            ReproducerInputSchema(jira_issue="RHEL-12345", package="../victim"),
            reproducer_config=None,
            available_tools=[],
            load_config_if_missing=False,
        )


@pytest.mark.asyncio
async def test_resolve_reproducer_setup_does_not_fetch_config_for_supplied_package():
    flexmock(r_agent).should_receive("run_tool").never()
    flexmock(agent_tasks).should_receive("fetch_reproducer_config").never()

    resolved_input, resolved_config, repository_url = await r_agent._resolve_reproducer_setup(
        ReproducerInputSchema(jira_issue="RHEL-12345", package="bind"),
        reproducer_config=None,
        available_tools=[],
        load_config_if_missing=False,
    )

    assert resolved_input.package == "bind"
    assert resolved_config is None
    assert repository_url == "https://gitlab.com/redhat/rhel/tests/bind"


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


@pytest.mark.parametrize(
    ("test_directory", "prefix", "expected"),
    [
        ("shared_pkg/exp_security/Security/CVE-1", "shared_pkg/exp_security", True),
        ("/shared_pkg/exp_security/Security/CVE-1", "shared_pkg/exp_security", True),
        ("shared_pkg/exp_security_extra/Security/CVE-1", "shared_pkg/exp_security", False),
        ("Security/CVE-1", "shared_pkg/exp_security", False),
        ("shared_pkg/../Security/CVE-1", "shared_pkg/exp_security", False),
        (None, "shared_pkg/exp_security", False),
    ],
)
def test_test_directory_is_under_prefix(tmp_path, test_directory, prefix, expected):
    tests_clone = tmp_path / "tests"
    tests_clone.mkdir()
    assert r_agent._test_directory_is_under_prefix(tests_clone, test_directory, prefix) is expected


def test_prefix_exception_requires_bootstrapped_adaptation(tmp_path: Path):
    result = _output(adapted_existing=True, test_directory="legacy/Security/CVE-2026-1")
    bootstrap_without_existing_test = PreparedTestsClone(tests_clone=tmp_path)
    bootstrap_with_existing_test = PreparedTestsClone(
        tests_clone=tmp_path,
        existing_test_directory="legacy/Security/CVE-2026-1",
    )

    assert not r_agent._is_bootstrapped_test_adaptation(result, bootstrap_without_existing_test)
    assert r_agent._is_bootstrapped_test_adaptation(result, bootstrap_with_existing_test)

    result.test_directory = "legacy/Security/CVE-2026-1/"
    assert r_agent._is_bootstrapped_test_adaptation(result, bootstrap_with_existing_test)

    result.test_directory = "other/Security/CVE-2026-1"
    assert not r_agent._is_bootstrapped_test_adaptation(result, bootstrap_with_existing_test)


def test_test_directory_is_under_prefix_rejects_symlinked_prefix(tmp_path: Path):
    tests_clone = tmp_path / "tests"
    (tests_clone / "shared").mkdir(parents=True)
    (tests_clone / "packages").mkdir()
    (tests_clone / "packages" / "bind").symlink_to(tests_clone / "shared", target_is_directory=True)

    assert (
        r_agent._test_directory_is_under_prefix(
            tests_clone,
            "packages/bind/Security/CVE-1",
            "packages/bind",
        )
        is False
    )


def test_test_directory_is_under_prefix_rejects_test_symlink_outside_prefix(tmp_path: Path):
    tests_clone = tmp_path / "tests"
    prefix = tests_clone / "packages" / "bind"
    target = tests_clone / "packages" / "openssl" / "Security"
    prefix.mkdir(parents=True)
    target.mkdir(parents=True)
    (prefix / "Security").symlink_to(target, target_is_directory=True)

    assert (
        r_agent._test_directory_is_under_prefix(
            tests_clone,
            "packages/bind/Security/CVE-1",
            "packages/bind",
        )
        is False
    )


def test_test_directory_is_under_prefix_accepts_test_symlink_inside_prefix(tmp_path: Path):
    tests_clone = tmp_path / "tests"
    prefix = tests_clone / "packages" / "bind"
    target = prefix / "real" / "Security"
    target.mkdir(parents=True)
    (prefix / "alias").symlink_to(prefix / "real", target_is_directory=True)

    assert r_agent._test_directory_is_under_prefix(
        tests_clone,
        "packages/bind/alias/Security/CVE-1",
        "packages/bind",
    )


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


def test_discover_existing_reproducer_test_dir_finds_prefixed_security_cve(tmp_path: Path):
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    canonical = repo / "shared_pkg" / "exp_security" / "Security" / "CVE-2026-50219"
    canonical.mkdir(parents=True)
    (canonical / "main.fmf").write_text("summary: test\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id="CVE-2026-50219",
        jira_issue="RHEL-220981",
        reproducer_type="cve",
        test_directory_prefix="shared_pkg/exp_security",
    )

    assert discovered == canonical


def test_discover_existing_reproducer_test_dir_finds_prefixed_regression(tmp_path: Path):
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    canonical = repo / "shared_pkg" / "exp_security" / "Regression" / "RHEL-100"
    canonical.mkdir(parents=True)
    (canonical / "main.fmf").write_text("summary: test\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id=None,
        jira_issue="RHEL-100",
        reproducer_type="bug",
        test_directory_prefix="shared_pkg/exp_security",
    )

    assert discovered == canonical


def test_discover_existing_reproducer_test_dir_finds_exact_root_cve_with_prefix(tmp_path: Path):
    repo = tmp_path / "tests-pkg"
    repo.mkdir()
    legacy = repo / "Security" / "CVE-2026-50219"
    legacy.mkdir(parents=True)
    (legacy / "main.fmf").write_text("summary: test\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id="CVE-2026-50219",
        jira_issue="RHEL-220981",
        reproducer_type="cve",
        test_directory_prefix="shared_pkg/exp_security",
    )

    assert discovered == legacy


def test_discover_shared_repo_ignores_root_cve_not_changed_by_mr(tmp_path: Path):
    repo = tmp_path / "tests-shared"
    legacy = repo / "Security" / "CVE-2026-50219"
    legacy.mkdir(parents=True)
    (legacy / "main.fmf").write_text("summary: another package's test\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id="CVE-2026-50219",
        jira_issue="RHEL-220981",
        reproducer_type="cve",
        test_directory_prefix="packages/expat",
        mr_changed_paths={"README"},
    )

    assert discovered is None


def test_discover_shared_repo_regression_reuse_requires_mr_changes(tmp_path: Path):
    repo = tmp_path / "tests-shared"
    root_test = repo / "Regression" / "RHEL-100"
    root_test.mkdir(parents=True)
    (root_test / "main.fmf").write_text("summary: root regression test\n")

    exact_match = _discover_existing_reproducer_test_dir(
        repo,
        cve_id=None,
        jira_issue="RHEL-100",
        reproducer_type="bug",
        mr_changed_paths={"README"},
    )
    assert exact_match is None

    prefix = "packages/expat"
    fallback_test = repo / prefix / "Regression" / "RHEL-101"
    fallback_test.mkdir(parents=True)
    (fallback_test / "main.fmf").write_text("summary: prefixed regression test\n")
    unrelated_mr_paths = {"README"}
    common = {
        "cve_id": None,
        "jira_issue": "RHEL-200",
        "reproducer_type": "bug",
        "test_directory_prefix": prefix,
    }

    assert (
        _discover_existing_reproducer_test_dir(
            repo,
            **common,
            mr_changed_paths=unrelated_mr_paths,
        )
        is None
    )
    assert (
        _discover_existing_reproducer_test_dir(
            repo,
            **common,
            mr_changed_paths={f"{prefix}/Regression/RHEL-101/main.fmf"},
        )
        == fallback_test
    )


def test_discover_existing_reproducer_test_dir_does_not_fallback_to_shared_repo_root(tmp_path: Path):
    repo = tmp_path / "tests-shared"
    unrelated = repo / "Regression" / "RHEL-100"
    unrelated.mkdir(parents=True)
    (unrelated / "main.fmf").write_text("summary: unrelated package test\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id=None,
        jira_issue="RHEL-200",
        reproducer_type="bug",
        test_directory_prefix="packages/bind",
    )

    assert discovered is None


def test_discover_existing_reproducer_test_dir_fallback_stays_under_prefix(tmp_path: Path):
    repo = tmp_path / "tests-shared"
    candidate = repo / "packages" / "bind" / "Regression" / "RHEL-100"
    candidate.mkdir(parents=True)
    (candidate / "main.fmf").write_text("summary: bind test\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id=None,
        jira_issue="RHEL-200",
        reproducer_type="bug",
        test_directory_prefix="packages/bind",
    )

    assert discovered == candidate


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


def test_discover_existing_reproducer_test_dir_skips_unscoped_custom_repo_fallback(tmp_path: Path):
    repo = tmp_path / "tests-shared"
    unrelated = repo / "Regression" / "RHEL-100"
    unrelated.mkdir(parents=True)
    (unrelated / "main.fmf").write_text("summary: unrelated package test\n")

    discovered = _discover_existing_reproducer_test_dir(
        repo,
        cve_id=None,
        jira_issue="RHEL-200",
        reproducer_type="bug",
        allow_unscoped_regression_fallback=False,
    )

    assert discovered is None


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
    matched = _match_open_reproducer_mr(mrs, package="expat", cve_ids=["CVE-2026-56132"])
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
    assert _match_open_reproducer_mr(mrs, package="expat", cve_ids=["CVE-2026-99999"]) is None
    assert _match_open_reproducer_mr(mrs, package="expat", cve_ids=["CVE-2026-56132"]) is not None


def test_match_open_reproducer_mr_ignores_bare_cve_token_without_brackets():
    mrs = [
        {
            "url": "https://gitlab.com/redhat/rhel/tests/expat/-/merge_requests/17",
            "title": "expat: add cve reproducer for RHEL-221017 (CVE-2026-56132)",
            "description": "Security test for CVE-2026-56132 in expat.",
        }
    ]
    assert _match_open_reproducer_mr(mrs, package="expat", cve_ids=["CVE-2026-56132"]) is None


def test_match_open_reproducer_mr_scopes_cve_and_existing_url_to_package():
    cve_id = "CVE-2026-56132"
    expat_mr = {
        "url": "https://gitlab.com/redhat/rhel/tests/shared/-/merge_requests/1",
        "title": f"expat: [{cve_id}] ymir reproducer test",
    }
    bind_mr = {
        "url": "https://gitlab.com/redhat/rhel/tests/shared/-/merge_requests/2",
        "title": f"bind: [{cve_id}] ymir reproducer test",
    }
    mrs = [expat_mr, bind_mr]

    assert _match_open_reproducer_mr(mrs, package="bind", cve_ids=[cve_id]) == bind_mr
    assert (
        _match_open_reproducer_mr(
            mrs,
            package="bind",
            cve_ids=[cve_id],
            existing_mr_url=expat_mr["url"],
        )
        == bind_mr
    )
    assert (
        _match_open_reproducer_mr(
            [expat_mr],
            package="bind",
            cve_ids=[cve_id],
            existing_mr_url=expat_mr["url"],
        )
        is None
    )


def test_match_regression_sibling_mr_when_issue_not_yet_in_title():
    mrs = [
        {
            "url": "https://gitlab.com/a/1",
            "title": "expat: [RHEL-100] ymir reproducer test",
        },
        {
            "url": "https://gitlab.com/a/2",
            "title": "bind: [RHEL-500] ymir reproducer test",
        },
    ]
    assert _match_regression_sibling_mr([mrs[0]], "RHEL-300", package="bind") is None
    assert _match_regression_sibling_mr(mrs, "RHEL-300", package="bind") == mrs[1]
    assert (
        _match_regression_sibling_mr(
            mrs,
            "RHEL-300",
            package="bind",
            clone_root="RHEL-500",
        )
        == mrs[1]
    )

    single = [mrs[0]]
    assert _match_regression_sibling_mr(single, "RHEL-200", package="bind") is None


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
    assert (
        _match_open_reproducer_mr(
            mrs,
            package="bind",
            jira_issue="RHEL-200",
            clone_root="RHEL-100",
        )
        == mrs[0]
    )
    assert (
        _match_open_reproducer_mr(
            mrs,
            package="bind",
            jira_issue="RHEL-600",
            clone_root="RHEL-500",
        )
        == mrs[1]
    )


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
async def test_match_open_reproducer_mr_for_input_uses_clone_root():
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

    async def _mock_resolve(*_args, **_kwargs):
        return "RHEL-100"

    flexmock(r_agent).should_receive("_resolve_reproducer_clone_root").replace_with(_mock_resolve)
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

    async def _mock_run_tool(name, available_tools=None, **_kwargs):
        if name == "list_project_merge_requests":
            return open_mrs
        raise AssertionError(name)

    async def _mock_resolve(*_args, **_kwargs):
        return "RHEL-100"

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(r_agent).should_receive("_resolve_reproducer_clone_root").replace_with(_mock_resolve)
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
    matched = _match_open_reproducer_mr(mrs, package="bind", jira_issue="RHEL-99999")
    assert matched is not None
    assert _match_open_reproducer_mr(mrs, package="bind", jira_issue="RHEL-88888") is None


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
        package="expat",
        cve_ids=["CVE-2026-56132"],
    )
    assert matched is not None

    matched2 = _match_open_reproducer_mr(
        [{"url": "https://gitlab.com/a/1", "title": title}],
        package="expat",
        cve_ids=["CVE-2026-50219"],
    )
    assert matched2 is not None


def test_build_mr_title_uses_jira_bracket_for_regression():
    result = _output(package="bind", reproducer_type="bug", jira_issue="RHEL-12345")
    agent_input = ReproducerInputSchema(jira_issue="RHEL-12345", package="bind")
    assert _build_mr_title(result, agent_input) == "bind: [RHEL-12345] ymir reproducer test"


def test_match_open_reproducer_mr_prefers_existing_url():
    mrs = [
        {
            "url": "https://gitlab.com/a/1",
            "title": "expat: [CVE-2026-12345] ymir reproducer test",
            "description": "",
        },
        {
            "url": "https://gitlab.com/a/2",
            "title": "bind: [CVE-2026-12345] ymir reproducer test",
            "description": "",
        },
    ]
    matched = _match_open_reproducer_mr(
        mrs,
        package="bind",
        cve_ids=["CVE-1"],
        existing_mr_url="https://gitlab.com/a/2",
    )
    assert matched["url"] == "https://gitlab.com/a/2"
    assert (
        _match_open_reproducer_mr(
            mrs,
            package="expat",
            cve_ids=["CVE-1"],
            existing_mr_url="https://gitlab.com/a/2",
        )
        is None
    )


@pytest.mark.asyncio
async def test_resolve_reproducer_mr_target_does_not_restore_bootstrap_rejection(tmp_path: Path):
    rejected_mr_url = "https://gitlab.com/redhat/rhel/tests/shared/-/merge_requests/10"
    result = _output(
        jira_issue="RHEL-10",
        success=True,
        package="bind",
        reproducer_type="cve",
        existing_mr_url=rejected_mr_url,
    )
    agent_input = ReproducerInputSchema(
        jira_issue="RHEL-10",
        package="bind",
        cve_id="CVE-2026-12345",
    )
    bootstrap = r_agent.TestsCloneBootstrap(tests_clone=tmp_path)

    flexmock(r_agent).should_receive("_list_open_reproducer_mrs").never()
    mr_url, branch, matched_mr = await _resolve_reproducer_mr_target(
        result,
        agent_input,
        "bind",
        [],
        bootstrap=bootstrap,
    )

    assert mr_url is None
    assert branch == "reproducer/RHEL-10"
    assert matched_mr is None
    assert result.existing_mr_url is None


@pytest.mark.asyncio
async def test_resolve_reproducer_mr_target_refuses_rejected_mr_branch_collision(tmp_path: Path):
    result = _output(
        jira_issue="RHEL-10",
        success=True,
        package="bind",
        reproducer_type="bug",
    )
    agent_input = ReproducerInputSchema(jira_issue="RHEL-10", package="bind")
    bootstrap = r_agent.TestsCloneBootstrap(
        tests_clone=tmp_path,
        rejected_mr_url="https://gitlab.com/redhat/rhel/tests/shared/-/merge_requests/10",
        rejected_mr_source_branch="reproducer/RHEL-10",
    )

    with pytest.raises(RuntimeError, match="Refusing to overwrite rejected open reproducer MR"):
        await _resolve_reproducer_mr_target(
            result,
            agent_input,
            "bind",
            [],
            bootstrap=bootstrap,
        )


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

    async def _mock_run_tool(name, available_tools=None, **_kwargs):
        if name == "list_project_merge_requests":
            return open_mrs
        raise AssertionError(name)

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)
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

    async def _mock_run_tool(name, available_tools=None, **_kwargs):
        if name == "list_project_merge_requests":
            return open_mrs
        raise AssertionError(name)

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)
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
@pytest.mark.parametrize(
    ("repository_url", "expected_branch"),
    [
        (None, "reproducer/RHEL-221017"),
        ("https://gitlab.com/redhat/rhel/tests/shared", "reproducer/expat/RHEL-221017"),
    ],
)
async def test_resolve_reproducer_mr_target_new_branch_without_open_mr(repository_url, expected_branch):
    result = _output(jira_issue="RHEL-221017", success=True, package="expat", reproducer_type="cve")
    agent_input = ReproducerInputSchema(
        jira_issue="RHEL-221017",
        package="expat",
        cve_id="CVE-2026-56132",
    )

    async def _mock_run_tool(name, available_tools=None, **_kwargs):
        if name == "list_project_merge_requests":
            return []
        raise AssertionError(name)

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    mr_url, branch, matched_mr = await _resolve_reproducer_mr_target(
        result,
        agent_input,
        "expat",
        [],
        repository_url=repository_url,
    )

    assert mr_url is None
    assert branch == expected_branch
    assert matched_mr is None
    assert result.adapted_existing is False


@pytest.mark.asyncio
async def test_resolve_reproducer_mr_target_propagates_incomplete_listing():
    result = _output(jira_issue="RHEL-221017", success=True, package="expat", reproducer_type="cve")
    agent_input = ReproducerInputSchema(
        jira_issue="RHEL-221017",
        package="expat",
        cve_id="CVE-2026-56132",
    )

    async def _incomplete_listing(*_args, **_kwargs):
        raise r_agent.IncompleteReproducerMRListingError("page two failed")

    flexmock(r_agent).should_receive("_list_open_reproducer_mrs").replace_with(_incomplete_listing)

    with pytest.raises(r_agent.IncompleteReproducerMRListingError, match="page two failed"):
        await _resolve_reproducer_mr_target(result, agent_input, "expat", [])


def test_reproducer_agent_enables_context_management():
    llm = flexmock()
    llm.allow_parallel_tool_calls = False
    flexmock(r_agent).should_receive("get_chat_model").and_return(llm)
    flexmock(r_agent).should_receive("is_reasoning_enabled").and_return(False)
    flexmock(r_agent).should_receive("get_tool_call_checker_config")

    agent = create_reproducer_agent(gateway_tools=[])
    assert agent._enable_context_management is True
    assert llm.allow_parallel_tool_calls is True


def test_reproducer_agent_exposes_authenticated_github_patch_tool(monkeypatch):
    captured = {}

    def reasoning_agent_factory(**kwargs):
        captured.update(kwargs)
        return flexmock()

    monkeypatch.setattr(r_agent, "ReasoningAgent", reasoning_agent_factory)
    monkeypatch.setattr(r_agent, "get_chat_model", lambda: flexmock(allow_parallel_tool_calls=False))
    monkeypatch.setattr(r_agent, "is_reasoning_enabled", lambda: False)
    monkeypatch.setattr(r_agent, "get_tool_call_checker_config", lambda: None)

    create_reproducer_agent(
        gateway_tools=[
            flexmock(name="get_patch_from_url"),
            flexmock(name="get_github_patch"),
            flexmock(name="get_github_pull_request"),
            flexmock(name="get_github_compare"),
        ]
    )

    tool_names = {tool.name for tool in captured["tools"]}
    assert {
        "get_patch_from_url",
        "get_github_patch",
        "get_github_pull_request",
        "get_github_compare",
    } <= tool_names


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


@pytest.mark.parametrize(
    ("existing_under_prefix", "test_directory_prefix"),
    [
        (True, "shared_pkg/exp_security"),
        (False, "shared_pkg/exp_security"),
        (False, None),
    ],
)
@pytest.mark.asyncio
async def test_bootstrap_tests_clone_checks_out_existing_mr_branch(
    tmp_path: Path,
    existing_under_prefix: bool,
    test_directory_prefix: str | None,
):
    working_dir = tmp_path / "Reproducer" / "RHEL-2"
    working_dir.mkdir(parents=True)
    repo = working_dir / "tests-bind"
    repo.mkdir()
    cve_id = "CVE-2026-0001"

    async def _mock_run_tool(name, available_tools=None, **_kwargs):
        if name == "clone_repository":
            await _git_init_with_main(repo)
            await check_subprocess(["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=repo)
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
            await check_subprocess(["git", "checkout", "-b", "reproducer/RHEL-1"], cwd=repo)
            mr_dir = repo / "Security" / cve_id
            if existing_under_prefix and test_directory_prefix:
                mr_dir = repo / test_directory_prefix / "Security" / cve_id
            mr_dir.mkdir(parents=True)
            (mr_dir / "main.fmf").write_text("summary: on mr\n")
            await check_subprocess(["git", "add", str(mr_dir.relative_to(repo))], cwd=repo)
            await check_subprocess(["git", "commit", "-m", "mr test"], cwd=repo)
            return "ok"
        raise AssertionError(f"unexpected tool {name}")

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    input_data = ReproducerInputSchema(
        jira_issue="RHEL-2",
        package="bind",
        cve_id=cve_id,
    )
    bootstrap = await _bootstrap_tests_clone(
        working_dir,
        input_data,
        [],
        test_directory_prefix=test_directory_prefix,
        require_mr_changed_paths=True,
    )

    assert bootstrap.existing_mr_url.endswith("/merge_requests/9")
    assert bootstrap.mr_source_branch == "reproducer/RHEL-1"
    expected_test_directory = f"Security/{cve_id}"
    if existing_under_prefix and test_directory_prefix:
        expected_test_directory = f"{test_directory_prefix}/{expected_test_directory}"
    assert bootstrap.existing_test_directory == expected_test_directory
    head, _ = await check_subprocess(["git", "branch", "--show-current"], cwd=repo)
    assert head.strip() == "reproducer/RHEL-1"


@pytest.mark.asyncio
async def test_bootstrap_discards_mr_when_branch_has_no_matching_test(tmp_path: Path):
    working_dir = tmp_path / "Reproducer" / "RHEL-4"
    working_dir.mkdir(parents=True)
    repo = working_dir / "tests-bind"
    repo.mkdir()
    cve_id = "CVE-2026-0002"
    test_directory_prefix = "packages/bind"

    async def _mock_run_tool(name, available_tools=None, **_kwargs):
        if name == "clone_repository":
            await _git_init_with_main(repo)
            return "ok"
        if name == "list_project_merge_requests":
            return [
                {
                    "url": "https://gitlab.com/redhat/rhel/tests/shared/-/merge_requests/10",
                    "title": f"bind: [{cve_id}] ymir reproducer test",
                    "source_branch": "reproducer/RHEL-3",
                }
            ]
        if name == "get_merge_request_details":
            return MergeRequestDetails(
                source_repo="https://gitlab.com/fork/tests-shared.git",
                source_branch="reproducer/RHEL-3",
                target_repo_name="shared",
                target_branch="main",
                title=f"bind: [{cve_id}] ymir reproducer test",
                description="",
                last_updated_at=datetime.now(UTC),
                comments=[],
            ).model_dump(mode="json")
        if name == "fetch_branch":
            await check_subprocess(["git", "checkout", "-b", "reproducer/RHEL-3"], cwd=repo)
            unrelated_dir = repo / test_directory_prefix / "Security" / "CVE-2026-0001"
            unrelated_dir.mkdir(parents=True)
            (unrelated_dir / "main.fmf").write_text("summary: unrelated test\n")
            await check_subprocess(["git", "add", test_directory_prefix], cwd=repo)
            await check_subprocess(["git", "commit", "-m", "unrelated test"], cwd=repo)
            return "ok"
        raise AssertionError(f"unexpected tool {name}")

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    input_data = ReproducerInputSchema(jira_issue="RHEL-4", package="bind", cve_id=cve_id)

    bootstrap = await _bootstrap_tests_clone(
        working_dir,
        input_data,
        [],
        test_directory_prefix=test_directory_prefix,
    )

    assert bootstrap.existing_mr_url is None
    assert bootstrap.mr_source_branch is None
    assert bootstrap.existing_test_directory is None
    assert bootstrap.matched_mr is None
    assert bootstrap.rejected_mr_url.endswith("/merge_requests/10")
    assert bootstrap.rejected_mr_source_branch == "reproducer/RHEL-3"
    head, _ = await check_subprocess(["git", "branch", "--show-current"], cwd=repo)
    assert head.strip() == "main"
    assert not (repo / test_directory_prefix / "Security" / "CVE-2026-0001").exists()


@pytest.mark.asyncio
async def test_bootstrap_tests_clone_uses_configured_repository_url(tmp_path: Path):
    working_dir = tmp_path / "Reproducer" / "RHEL-3"
    working_dir.mkdir(parents=True)
    repository_url = "https://gitlab.com/redhat/rhel/tests/shared.git"
    cloned_urls = []

    async def _mock_run_tool(name, available_tools=None, **kwargs):
        if name == "clone_repository":
            cloned_urls.append(kwargs["repository"])
            return "ok"
        if name == "list_project_merge_requests":
            assert kwargs["project"] == "redhat/rhel/tests/shared"
            return []
        raise AssertionError(f"unexpected tool {name}")

    flexmock(r_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    input_data = ReproducerInputSchema(jira_issue="RHEL-3", package="bind")

    await _bootstrap_tests_clone(working_dir, input_data, [], repository_url=repository_url)

    assert cloned_urls == [repository_url]


@pytest.mark.asyncio
async def test_bootstrap_rejects_clone_destination_symlink_outside_issue_workspace(tmp_path: Path):
    working_dir = tmp_path / "Reproducer" / "RHEL-3"
    working_dir.mkdir(parents=True)
    outside = tmp_path / "victim"
    outside.mkdir()
    (working_dir / "tests-bind").symlink_to(outside, target_is_directory=True)
    flexmock(r_agent).should_receive("run_tool").never()

    with pytest.raises(ValueError, match="Tests clone destination escapes issue workspace"):
        await _bootstrap_tests_clone(
            working_dir,
            ReproducerInputSchema(jira_issue="RHEL-3", package="bind"),
            [],
        )


# =============================================================================
# process_task tests
# =============================================================================


async def _async_noop(*_args, **_kwargs):
    pass


def _make_reproducer_payload(issue: str = "RHEL-99999", user_triggered: bool = False) -> bytes:
    input_data = ReproducerInputSchema(jira_issue=issue, package="bind")
    task = Task(metadata=input_data.model_dump(), user_triggered=user_triggered)
    return task.model_dump_json().encode()


@pytest.fixture
def _mock_reproducer_config_enabled():
    @contextlib.asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield []

    async def _mock_config(*_args, **_kwargs):
        return flexmock(enabled=True)

    flexmock(agent_tasks).should_receive("fetch_reproducer_config").replace_with(_mock_config)
    flexmock(agent_tasks).should_receive("set_jira_labels").replace_with(_async_noop)
    flexmock(agent_tasks).should_receive("post_user_ack_once").replace_with(_async_noop)
    flexmock(r_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)


@pytest.fixture
def _mock_workflow_lock():
    async def _mock_resolve_lock(*_args, **_kwargs):
        return "RHEL-99999"

    async def _mock_acquire_lock(*_args, **_kwargs):
        return '{"package":"bind","lock_id":"RHEL-99999","jira_issue":"RHEL-99999"}'

    async def _mock_release_lock(*_args, **_kwargs):
        return True

    flexmock(r_agent).should_receive("resolve_reproducer_lock_id").replace_with(_mock_resolve_lock)
    flexmock(r_agent).should_receive("try_acquire_reproducer_lock").replace_with(_mock_acquire_lock)
    flexmock(r_agent).should_receive("release_reproducer_lock").replace_with(_mock_release_lock)


@pytest.fixture
def _mock_env_vars(monkeypatch):
    monkeypatch.setenv("COLLECTOR_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")


async def _run_process_task(payload: bytes) -> None:
    """Run reproducer main() in queue mode, invoking process_task with payload once.

    process_task is a closure defined inside main() and cannot be imported directly.
    This helper runs main() with a fake run_task_loop that calls process_fn(payload)
    immediately, so process_task executes inside main()'s redis context — matching
    the production execution environment.  Test-specific patches (e.g. get_jira_issue_metadata,
    run_workflow) must be applied by the caller before invoking this helper.
    """
    transaction = flexmock()
    transaction.should_receive("__enter__").and_return(None)
    transaction.should_receive("__exit__").and_return(False)

    span_processor = flexmock()
    span_processor.should_receive("start_transaction").and_return(transaction)

    async def _mock_run_task_loop(_redis, _queues, process_fn, **_kw):
        await process_fn(payload)

    @contextlib.asynccontextmanager
    async def _mock_redis_client(*_args, **_kwargs):
        redis_mock = flexmock()
        redis_mock.should_receive("lpush").replace_with(_async_noop)

        async def _empty_error_list(*_args):
            return []

        redis_mock.should_receive("lrange").replace_with(_empty_error_list)
        yield redis_mock

    flexmock(r_agent).should_receive("init_sentry")
    flexmock(r_agent).should_receive("configure_logging")
    flexmock(r_agent).should_receive("resolve_chat_model_override")
    flexmock(r_agent).should_receive("setup_observability").and_return(span_processor)
    flexmock(r_agent).should_receive("run_task_loop").replace_with(_mock_run_task_loop)
    flexmock(r_agent).should_receive("redis_client").replace_with(_mock_redis_client)

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
async def test_process_task_skips_duplicate_with_terminal_label(_mock_env_vars, terminal_label):
    """When a terminal label is already set and the task is not user-triggered,
    process_task must skip without calling run_workflow.

    This is a regression guard for the get_jira_labels → get_jira_issue_metadata
    rename: the function must be called and its tuple return value unpacked correctly.
    """

    async def _mock_issue(*_args, **_kwargs):
        return [terminal_label], "New"

    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").once().with_args(
        "RHEL-99999"
    ).replace_with(_mock_issue)
    flexmock(r_agent).should_receive("run_workflow").never()

    await _run_process_task(_make_reproducer_payload())


@pytest.mark.asyncio
async def test_process_task_proceeds_despite_terminal_label_when_user_triggered(
    _mock_env_vars, _mock_reproducer_config_enabled, _mock_workflow_lock
):
    """A user-triggered run must always proceed even when a terminal label is set."""

    async def _mock_jira_metadata(*_args, **_kwargs):
        return ["ymir_reproducer_created"], "New"

    async def _mock_workflow(*_args, **_kwargs):
        return flexmock(result=_output())

    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(r_agent).should_receive("run_workflow").replace_with(_mock_workflow).once()
    await _run_process_task(_make_reproducer_payload(user_triggered=True))


@pytest.mark.asyncio
async def test_process_task_proceeds_when_terminal_label_and_in_progress(
    _mock_env_vars, _mock_reproducer_config_enabled, _mock_workflow_lock
):
    """If the in-progress label is set alongside a terminal label, the task
    must still be processed — the in-progress label signals an active run."""

    async def _mock_jira_metadata(*_args, **_kwargs):
        return ["ymir_reproducer_created", "ymir_reproducer_in_progress"], "New"

    async def _mock_workflow(*_args, **_kwargs):
        return flexmock(result=_output())

    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(r_agent).should_receive("run_workflow").once().replace_with(_mock_workflow)
    await _run_process_task(_make_reproducer_payload())


@pytest.mark.asyncio
async def test_process_task_proceeds_when_no_terminal_labels(
    _mock_env_vars, _mock_reproducer_config_enabled, _mock_workflow_lock
):
    """An issue with no terminal labels goes through the full workflow."""

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    async def _mock_workflow(*_args, **_kwargs):
        return flexmock(result=_output())

    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(r_agent).should_receive("run_workflow").once().replace_with(_mock_workflow)
    await _run_process_task(_make_reproducer_payload())


@pytest.mark.asyncio
async def test_process_task_delays_retry_for_incomplete_mr_listing(
    _mock_env_vars,
    _mock_reproducer_config_enabled,
    _mock_workflow_lock,
    monkeypatch,
):
    monkeypatch.setenv("REPRODUCER_RETRY_DELAY_SECONDS", "12")
    scheduled = []

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    async def _incomplete_mr_lookup(*_args, **_kwargs):
        raise r_agent.IncompleteReproducerMRListingError("GitLab MR listing unavailable")

    async def _capture_schedule(_redis, queue, payload, delay_seconds):
        scheduled.append((queue, payload, delay_seconds))

    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(r_agent).should_receive("run_workflow").once().replace_with(_incomplete_mr_lookup)
    flexmock(r_agent).should_receive("schedule_task").once().replace_with(_capture_schedule)

    await _run_process_task(_make_reproducer_payload())

    assert len(scheduled) == 1
    queue, payload, delay_seconds = scheduled[0]
    assert queue == RedisQueues.REPRODUCER_DELAYED_QUEUE.value
    assert Task.model_validate_json(payload).attempts == 1
    assert delay_seconds == 12


@pytest.mark.asyncio
async def test_process_task_passes_reproducer_config_to_workflow(_mock_env_vars, _mock_workflow_lock):
    config = PackageReproducerConfig(
        enabled=True,
        fmf={"require": ["runtime-package"]},
    )

    async def _mock_config(*_args, **_kwargs):
        return config

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    captured = {}

    async def _mock_workflow(*_args, **kwargs):
        captured.update(kwargs)
        return flexmock(result=_output(jira_issue="RHEL-99999", package="bind"))

    @contextlib.asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield []

    flexmock(agent_tasks).should_receive("fetch_reproducer_config").replace_with(_mock_config)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(agent_tasks).should_receive("set_jira_labels").replace_with(_async_noop)
    flexmock(agent_tasks).should_receive("post_user_ack_once").replace_with(_async_noop)
    flexmock(r_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(r_agent).should_receive("run_workflow").once().replace_with(_mock_workflow)

    await _run_process_task(_make_reproducer_payload())

    assert captured["reproducer_config"] is config


@pytest.mark.asyncio
async def test_process_task_blocks_when_workflow_lock_busy(_mock_env_vars, _mock_reproducer_config_enabled):
    """Busy create/adapt locks park the task until the holder releases."""

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    async def _mock_resolve_lock(*_args, **_kwargs):
        return "CVE-2026-56132"

    async def _mock_acquire_lock(*_args, **_kwargs):
        return None

    async def _mock_enqueue_task(*_args, **_kwargs):
        pass

    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(r_agent).should_receive("run_workflow").never()
    flexmock(r_agent).should_receive("resolve_reproducer_lock_id").replace_with(_mock_resolve_lock)
    flexmock(r_agent).should_receive("try_acquire_reproducer_lock").replace_with(_mock_acquire_lock)
    flexmock(r_agent).should_receive("enqueue_blocked_reproducer_task").once().replace_with(
        _mock_enqueue_task
    )
    await _run_process_task(_make_reproducer_payload())


# -- fetch_reproducer_config ---------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_reproducer_config_returns_default_when_not_found():
    async def _mock_run_tool(*_args, **_kwargs):
        return "No maintainer rules found for package 'bind' (file 'ymir.yaml' not found)"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_reproducer_config("bind", [])

    assert config.enabled is False


@pytest.mark.asyncio
async def test_fetch_reproducer_config_parses_enabled():
    async def _mock_run_tool(*_args, **_kwargs):
        return "reproducer:\n  enabled: true\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_reproducer_config("bind", [])

    assert config.enabled is True


@pytest.mark.asyncio
async def test_fetch_reproducer_config_parses_fmf_metadata():
    async def _mock_run_tool(*_args, **_kwargs):
        return (
            "reproducer:\n"
            "  enabled: true\n"
            "  fmf:\n"
            "    require: [runtime-package]\n"
            "    recommend: [optional-tool]\n"
            "    environment:\n"
            "      PACKAGE_SETTING: value\n"
            "    duration: 30m\n"
            "    tier: '1'\n"
            "    tag: [package-specific]\n"
            "  context:\n"
            "    by_fix_version:\n"
            "      rhel-10.3:\n"
            "        runtime_version: '3.3'\n"
        )

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_reproducer_config("bind", [])

    assert config.fmf.require == ["runtime-package"]
    assert config.fmf.recommend == ["optional-tool"]
    assert config.fmf.environment == {"PACKAGE_SETTING": "value"}
    assert config.fmf.duration == "30m"
    assert config.fmf.tier == "1"
    assert config.fmf.tag == ["package-specific"]
    assert config.context.by_fix_version == {"rhel-10.3": {"runtime_version": "3.3"}}


@pytest.mark.asyncio
async def test_fetch_reproducer_config_rejects_unknown_fmf_metadata():
    async def _mock_run_tool(*_args, **_kwargs):
        return "reproducer:\n  enabled: true\n  fmf:\n    unsupported: value\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)

    with pytest.raises(InvalidReproducerConfigError, match="malformed"):
        await fetch_reproducer_config("bind", [])


def test_render_prompt_includes_package_fmf_metadata():
    input_data = ReproducerInputSchema(
        jira_issue="RHEL-12345",
        package="bind",
        fix_version="rhel-10.3",
    )
    config = PackageReproducerConfig(
        enabled=True,
        test_directory_prefix="packages/bind",
        fmf={
            "require": ["runtime-package"],
            "recommend": ["optional-tool"],
            "environment": {"PACKAGE_SETTING": "${runtime_version}"},
            "duration": "30m",
        },
        context={"by_fix_version": {"rhel-10.3": {"runtime_version": "3.3"}}},
    )

    prompt = _render_prompt(input_data, reproducer_config=config)

    assert "Package-specific FMF metadata from `ymir.yaml`" in prompt
    assert '"runtime-package"' in prompt
    assert '"optional-tool"' in prompt
    assert '"PACKAGE_SETTING": "3.3"' in prompt
    assert '"duration": "30m"' in prompt
    assert "${runtime_version}" not in prompt
    assert "Configured test directory prefix: `packages/bind`." in prompt


def test_render_prompt_passes_configured_test_repository_to_template(monkeypatch):
    captured = {}
    config = PackageReproducerConfig(
        enabled=True,
        test_repository="https://gitlab.com/redhat/rhel/tests/shared.git",
        test_directory_prefix="packages/bind",
    )

    def _capture_context(_template_name, context):
        captured["context"] = context
        return ""

    monkeypatch.setattr(r_agent, "render_template", _capture_context)
    _render_prompt(
        ReproducerInputSchema(jira_issue="RHEL-12345", package="bind"),
        reproducer_config=config,
    )

    assert captured["context"].test_repository_url == config.test_repository
    assert captured["context"].test_directory_prefix == config.test_directory_prefix


def test_render_fmf_templates_rejects_unknown_variable():
    with pytest.raises(ValueError, match="Unknown FMF context variable: missing"):
        _render_fmf_templates({"environment": {"SETTING": "${missing}"}}, {})


@pytest.mark.asyncio
async def test_fetch_reproducer_config_parses_disabled():
    async def _mock_run_tool(*_args, **_kwargs):
        return "reproducer:\n  enabled: false\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_reproducer_config("bind", [])

    assert config.enabled is False


@pytest.mark.asyncio
async def test_fetch_reproducer_config_raises_on_malformed_section():
    async def _mock_run_tool(*_args, **_kwargs):
        return "reproducer:\n  enabled: not_a_bool\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)

    with pytest.raises(InvalidReproducerConfigError, match="malformed"):
        await fetch_reproducer_config("bind", [])


@pytest.mark.asyncio
async def test_process_task_skips_when_reproducer_disabled(_mock_env_vars):

    @contextlib.asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield []

    async def _disabled_config(*_args, **_kwargs):
        return flexmock(enabled=False)

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    flexmock(agent_tasks).should_receive("fetch_reproducer_config").replace_with(_disabled_config)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(r_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(r_agent).should_receive("run_workflow").never()
    flexmock(r_agent).should_receive("try_acquire_reproducer_lock").never()
    await _run_process_task(_make_reproducer_payload())


@pytest.mark.asyncio
async def test_run_tmt_lint_passes(tmp_path, monkeypatch):
    tests_clone = tmp_path / "tests-bind"
    test_dir = tests_clone / "Regression" / "RHEL-1"
    test_dir.mkdir(parents=True)
    (test_dir / "main.fmf").write_text("summary: demo\ntest: ./runtest.sh\n")
    (tests_clone / ".fmf").mkdir()
    (tests_clone / ".fmf" / "version").write_text("1\n")

    async def _fake_run_subprocess(cmd, shell=False, cwd=None, env=None):
        assert cmd == ["tmt", "lint", "/Regression/RHEL-1"]
        assert cwd == tests_clone
        return 0, "pass\n", None

    monkeypatch.setattr(r_agent, "run_subprocess", _fake_run_subprocess)
    ok, output = await r_agent.run_tmt_lint(tests_clone, "Regression/RHEL-1")
    assert ok is True
    assert "pass" in output


@pytest.mark.asyncio
async def test_run_tmt_lint_fails_with_output(tmp_path, monkeypatch):
    tests_clone = tmp_path / "tests-bind"
    test_dir = tests_clone / "Regression" / "RHEL-1"
    test_dir.mkdir(parents=True)
    (test_dir / "main.fmf").write_text("summary: demo\n")

    async def _fake_run_subprocess(cmd, shell=False, cwd=None, env=None):
        return 1, "error: missing test key\n", None

    monkeypatch.setattr(r_agent, "run_subprocess", _fake_run_subprocess)
    ok, output = await r_agent.run_tmt_lint(tests_clone, "Regression/RHEL-1")
    assert ok is False
    assert "missing test key" in output
    assert (tests_clone / ".fmf" / "version").is_file()


@pytest.mark.asyncio
async def test_run_tmt_lint_missing_metadata(tmp_path):
    tests_clone = tmp_path / "tests-bind"
    test_dir = tests_clone / "Regression" / "RHEL-1"
    test_dir.mkdir(parents=True)
    # Dir must have at least one file so _resolve_test_dir doesn't return None
    (test_dir / "runtest.sh").write_text("#!/bin/bash\nexit 1\n")
    ok, output = await r_agent.run_tmt_lint(tests_clone, "Regression/RHEL-1")
    assert ok is False
    assert "no FMF metadata" in output


@pytest.mark.asyncio
async def test_run_tmt_lint_no_tests_found(tmp_path, monkeypatch):
    """tmt lint exit 0 with 'no tests found' must be treated as failure."""
    tests_clone = tmp_path / "tests-bind"
    test_dir = tests_clone / "Regression" / "RHEL-1"
    test_dir.mkdir(parents=True)
    (test_dir / "main.fmf").write_text("summary: broken\n")
    (tests_clone / ".fmf").mkdir()
    (tests_clone / ".fmf" / "version").write_text("1\n")

    async def _fake_run_subprocess(cmd, shell=False, cwd=None, env=None):
        return 0, "No tests found\n", None

    monkeypatch.setattr(r_agent, "run_subprocess", _fake_run_subprocess)
    ok, output = await r_agent.run_tmt_lint(tests_clone, "Regression/RHEL-1")
    assert ok is False
    assert "found no test" in output
