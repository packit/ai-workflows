"""Unit tests for reproducer agent label and comment helpers."""

from pathlib import Path

import pytest

from ymir.agents.reproducer_agent import (
    _determine_comment_resolution,
    _determine_result_label,
    _needs_merge_request,
    _resolve_test_dir,
    _should_finalize_jira,
)
from ymir.common.constants import JiraLabels
from ymir.common.models import ReproducerOutputSchema


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
