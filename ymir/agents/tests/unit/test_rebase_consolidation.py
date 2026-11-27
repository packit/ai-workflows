import pytest
from flexmock import flexmock

from ymir.agents import rebase_agent
from ymir.agents.rebase_agent import _clear_rebase_resolved_errors, _consolidated_issue_keys
from ymir.agents.rebase_consolidation import (
    add_jira_tickets_to_latest_changelog_entry,
    changelog_entry_headers,
    has_new_latest_changelog_entry,
    uses_autochangelog,
)
from ymir.common.models import ConsolidatedIssue, RebaseData


def _spec(changelog: str = "", changelog_directive: str = "%changelog") -> str:
    return (
        "Name: package\n"
        "Version: 1\n"
        "Release: 1\n"
        "Summary: Test package\n"
        "License: MIT\n"
        "%description\n"
        "Test package\n"
        f"{changelog_directive}\n"
        f"{changelog}"
    )


def test_add_jira_tickets_to_latest_changelog_entry(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(
        _spec(
            "* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n"
            "- Update package\n"
            "- Resolves: RHEL-100\n"
            "* Sun Sep 13 2026 Maintainer <maintainer@example.com> - 0-1\n"
            "- Previous update\n"
        )
    )

    assert add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200", "RHEL-300"])
    assert spec.read_text() == _spec(
        "* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n"
        "- Update package\n"
        "- Resolves: RHEL-100\n"
        "- Resolves: RHEL-200\n"
        "- Resolves: RHEL-300\n"
        "* Sun Sep 13 2026 Maintainer <maintainer@example.com> - 0-1\n"
        "- Previous update\n"
    )


def test_add_jira_tickets_recognizes_unusual_separator_in_resolves_footer(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(
        _spec("* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n- Resolves: RHEL-100 and RHEL-200\n")
    )
    original = spec.read_text()

    assert not add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200"])
    assert spec.read_text() == original


def test_add_jira_tickets_recognizes_and_extends_suffix_references(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(
        _spec("* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n- Update package (RHEL-100/RHEL-200)\n")
    )

    assert add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200", "RHEL-300"])
    assert spec.read_text().endswith("- Update package (RHEL-100/RHEL-200/RHEL-300)\n")


def test_add_jira_tickets_recognizes_and_extends_inline_resolves_reference(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(
        _spec("* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n- Resolves: RHEL-100 - Update package\n")
    )

    assert add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200"])
    assert spec.read_text().endswith("- Resolves: RHEL-100, RHEL-200 - Update package\n")


def test_add_jira_tickets_preserves_suffix_trailing_whitespace(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(
        _spec("* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n- Update package (RHEL-100/RHEL-200)   \n")
    )

    assert add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200", "RHEL-300"])
    assert spec.read_text().endswith("- Update package (RHEL-100/RHEL-200/RHEL-300)   \n")


def test_add_jira_tickets_skips_empty_changelog(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(_spec())

    assert not add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200"])
    assert spec.read_text() == _spec()


def test_uses_autochangelog(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(_spec("%autochangelog\n"))

    assert uses_autochangelog(spec)


def test_does_not_treat_commented_autochangelog_as_a_directive(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(_spec("# %autochangelog\n"))

    assert not uses_autochangelog(spec)


def test_new_changelog_entry_is_required_before_updating_consolidation_metadata(tmp_path):
    spec = tmp_path / "package.spec"
    original = _spec("* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n- Historical release metadata\n")
    spec.write_text(original)

    headers_before = changelog_entry_headers(spec)

    assert not has_new_latest_changelog_entry(spec, headers_before)
    with pytest.raises(ValueError, match="new changelog entry"):
        add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200"], headers_before)
    assert spec.read_text() == original


def test_add_jira_ticket_preserves_canonical_changelog_title(tmp_path):
    spec = tmp_path / "package.spec"
    old_entry = "* Sun Sep 13 2026 Maintainer <maintainer@example.com> - 0-1\n- Previous update\n"
    spec.write_text(_spec(old_entry))
    headers_before = changelog_entry_headers(spec)
    canonical_title = "CVE-2026-1234 package: Fix issue"
    spec.write_text(
        _spec(f"* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n- {canonical_title}\n" + old_entry)
    )

    assert add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100"], headers_before)
    assert f"- {canonical_title}\n- Resolves: RHEL-100\n" in spec.read_text()


def test_add_jira_tickets_does_not_treat_prose_as_a_reference(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(
        _spec(
            "* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n"
            "- Fix the issue reported in RHEL-100\n"
            "- Related: RHEL-200\n"
        )
    )

    assert add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200"])
    assert spec.read_text().endswith(
        "- Fix the issue reported in RHEL-100\n"
        "- Related: RHEL-200\n"
        "- Resolves: RHEL-100\n"
        "- Resolves: RHEL-200\n"
    )


def test_add_jira_tickets_does_not_rewrite_related_prose_suffix(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(
        _spec("* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n- Related: issue context (RHEL-200)\n")
    )

    assert add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100"])
    assert spec.read_text().endswith("- Related: issue context (RHEL-200)\n- Resolves: RHEL-100\n")


def test_add_jira_tickets_preserves_star_bulleted_resolves_reference(tmp_path):
    spec = tmp_path / "package.spec"
    spec.write_text(
        _spec(
            "* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n"
            "  * Resolves: RHEL-200\n"
            "* Sun Sep 13 2026 Maintainer <maintainer@example.com> - 0-1\n"
            "- Previous update\n"
        )
    )

    assert add_jira_tickets_to_latest_changelog_entry(spec, ["RHEL-100", "RHEL-200"])
    assert spec.read_text() == _spec(
        "* Mon Sep 14 2026 Ymir <ymir@example.com> - 1-1\n"
        "  * Resolves: RHEL-200\n"
        "  * Resolves: RHEL-100\n"
        "* Sun Sep 13 2026 Maintainer <maintainer@example.com> - 0-1\n"
        "- Previous update\n"
    )


def test_consolidated_issue_keys_deduplicates_siblings_and_primary():
    assert _consolidated_issue_keys(
        "RHEL-100",
        [
            ConsolidatedIssue(issue_key="RHEL-200"),
            ConsolidatedIssue(issue_key="RHEL-100"),
            ConsolidatedIssue(issue_key="RHEL-200"),
        ],
    ) == ["RHEL-100", "RHEL-200"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_successful_consolidated_rebase_clears_primary_and_distinct_siblings(dry_run):
    calls = []

    async def mock_clear(redis_conn, issue, queue, *, target_branch, dry_run):
        calls.append((redis_conn, issue, queue, target_branch, dry_run))

    flexmock(rebase_agent).should_receive("clear_resolved_errors").replace_with(mock_clear).once()
    redis = object()
    data = RebaseData(
        package="expat",
        version="2.7.0",
        jira_issue="RHEL-100",
        consolidated_issues=[
            ConsolidatedIssue(issue_key="RHEL-200"),
            ConsolidatedIssue(issue_key="RHEL-100"),
            ConsolidatedIssue(issue_key="RHEL-200"),
            ConsolidatedIssue(issue_key="RHEL-300"),
        ],
    )

    await _clear_rebase_resolved_errors(
        redis, data, "rebase_queue_c10s", target_branch="rhel-10.3", dry_run=dry_run
    )

    assert calls == [(redis, ["RHEL-100", "RHEL-200", "RHEL-300"], "rebase_queue_c10s", "rhel-10.3", dry_run)]
