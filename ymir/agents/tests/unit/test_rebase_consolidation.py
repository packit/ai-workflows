import pytest
from flexmock import flexmock

from ymir.agents import rebase_agent, rebase_consolidation
from ymir.agents.rebase_agent import _clear_rebase_resolved_errors, _consolidated_issue_keys
from ymir.agents.rebase_consolidation import (
    add_jira_tickets_to_latest_changelog_entry,
    build_siblings_jql,
    changelog_entry_headers,
    find_triaged_rebase_siblings,
    has_new_latest_changelog_entry,
    uses_autochangelog,
)
from ymir.common.models import ConsolidatedIssue, RebaseData
from ymir.common.utils import extract_text_from_adf


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


def test_build_siblings_jql():
    jql = build_siblings_jql("RHEL-100", "dotnet10.0", "rhel-10.2")
    assert 'component = "dotnet10.0"' in jql
    assert 'fixVersion in ("rhel-10.2", "rhel-10.2.z")' in jql
    assert 'key != "RHEL-100"' in jql
    assert 'labels = "SecurityTracking"' in jql
    assert 'status in ("New", "Planning")' in jql


def test_build_siblings_jql_escapes_component_quotes():
    jql = build_siblings_jql("RHEL-100", 'comp"name', "rhel-9.8.z")
    assert r'component = "comp\"name"' in jql
    assert 'fixVersion in ("rhel-9.8", "rhel-9.8.z")' in jql


def test_build_siblings_jql_filters_modular_stream():
    """Modular issues must filter on Downstream Component Name to avoid mixing streams."""
    jql = build_siblings_jql("RHEL-100", "postgis", "rhel-9.8", downstream_component="postgresql:16/postgis")
    assert 'cf[10669] = "postgresql:16/postgis"' in jql
    assert 'component = "postgis"' in jql


def test_build_siblings_jql_no_filter_for_nonmodular():
    """Non-modular issues should not add a Downstream Component Name filter."""
    jql = build_siblings_jql("RHEL-100", "curl", "rhel-9.8", downstream_component="curl")
    assert "cf[10669]" not in jql


def test_build_siblings_jql_no_filter_when_none():
    """When downstream_component is None, no extra filter is added."""
    jql = build_siblings_jql("RHEL-100", "curl", "rhel-9.8", downstream_component=None)
    assert "cf[10669]" not in jql


class TestSiblingCommentExtraction:
    """Tests for extracting and matching sibling references from Jira comments."""

    def test_extract_from_adf_inline_card(self):
        """Test extraction from ADF JSON with inlineCard (MCP gateway format)."""
        adf_body = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Queued for triage as potential sibling of "},
                        {
                            "type": "inlineCard",
                            "attrs": {
                                "url": "https://redhat.atlassian.net/browse/RHEL-234905#icft=RHEL-234905"
                            },
                        },
                    ],
                }
            ],
        }

        result = extract_text_from_adf(adf_body)

        # Should extract URL from inlineCard
        assert "https://redhat.atlassian.net/browse/RHEL-234905" in result
        # Should contain the issue key
        assert "RHEL-234905" in result
        # Should contain the phrase
        assert "Queued for triage as potential sibling of" in result

    def test_extract_from_html_smartlink(self):
        """Test extraction from HTML with smartlink tags (alternative MCP format)."""
        html_body = (
            "Queued for triage as potential sibling of "
            '<custom data-type="smartlink" data-id="id-0">'
            "https://redhat.atlassian.net/browse/RHEL-234905#icft=RHEL-234905"
            "</custom>"
        )

        result = extract_text_from_adf(html_body)

        # Should extract issue key from smartlink URL
        assert "RHEL-234905" in result
        # Should contain the phrase
        assert "Queued for triage as potential sibling of" in result

    def test_sibling_comment_matching_with_inline_card(self):
        """Test that sibling comment check works with ADF inlineCard format."""
        primary_issue = "RHEL-234905"
        comment_body = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Queued for triage as potential sibling of "},
                        {
                            "type": "inlineCard",
                            "attrs": {"url": f"https://redhat.atlassian.net/browse/{primary_issue}"},
                        },
                    ],
                }
            ],
        }

        extracted = extract_text_from_adf(comment_body)

        # This is the check pattern used in find_triaged_rebase_siblings
        has_phrase = "Queued for triage as potential sibling of" in extracted
        has_issue_key = primary_issue in extracted

        assert has_phrase, "Should find sibling phrase in extracted text"
        assert has_issue_key, "Should find primary issue key in extracted text"
        assert has_phrase and has_issue_key, "Both conditions should be true for valid sibling reference"

    def test_sibling_comment_matching_with_wrong_primary(self):
        """Test that sibling comment check fails when issue key doesn't match."""
        primary_issue = "RHEL-234905"
        wrong_issue = "RHEL-999999"
        comment_body = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Queued for triage as potential sibling of "},
                        {
                            "type": "inlineCard",
                            "attrs": {"url": f"https://redhat.atlassian.net/browse/{wrong_issue}"},
                        },
                    ],
                }
            ],
        }

        extracted = extract_text_from_adf(comment_body)

        has_phrase = "Queued for triage as potential sibling of" in extracted
        has_issue_key = primary_issue in extracted

        assert has_phrase, "Should find sibling phrase"
        assert not has_issue_key, "Should NOT find wrong issue key"
        assert not (has_phrase and has_issue_key), "Should fail the combined check"

    def test_sibling_comment_matching_with_plain_text(self):
        """Test that sibling comment check works with plain text (fallback)."""
        primary_issue = "RHEL-234905"
        plain_text = f"Queued for triage as potential sibling of {primary_issue}"

        extracted = extract_text_from_adf(plain_text)

        has_phrase = "Queued for triage as potential sibling of" in extracted
        has_issue_key = primary_issue in extracted

        assert has_phrase and has_issue_key, "Should work with plain text format"

    def test_extract_multiple_inline_cards(self):
        """Test extraction handles multiple inlineCard nodes correctly."""
        adf_body = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "See "},
                        {"type": "inlineCard", "attrs": {"url": "https://example.com/browse/RHEL-100"}},
                        {"type": "text", "text": " and "},
                        {"type": "inlineCard", "attrs": {"url": "https://example.com/browse/RHEL-200"}},
                    ],
                }
            ],
        }

        result = extract_text_from_adf(adf_body)

        # Should extract both URLs
        assert "RHEL-100" in result
        assert "RHEL-200" in result
        assert "See" in result
        assert "and" in result


@pytest.mark.asyncio
async def test_find_triaged_rebase_siblings_no_unbound_error_on_jira_failure():
    """Regression: primary_details must be initialised before try/except.

    When get_jira_details fails, the except block logs 'proceeding anyway'.
    The code then checks ``if downstream_component is None and primary_details:``.
    Without ``primary_details = None`` before try, this raises UnboundLocalError.
    """
    rebase_data = RebaseData(
        package="postgis",
        version="3.5.2",
        jira_issue="RHEL-250764",
        fix_version="rhel-9.8.z",
    )

    # Simulate get_jira_details failure (MCP gateway down, network error, etc.)
    flexmock(rebase_consolidation).should_receive("run_tool").and_raise(Exception("MCP Gateway unreachable"))

    # Should NOT raise UnboundLocalError — should return empty results
    result, summary = await find_triaged_rebase_siblings(
        jira_issue="RHEL-250764",
        rebase_data=rebase_data,
        available_tools=[],
        downstream_component=None,
    )
    assert result == []
    assert summary == ""


@pytest.mark.asyncio
async def test_find_triaged_rebase_siblings_requires_exact_primary_key(monkeypatch):
    async def run_tool(name, **kwargs):
        if name == "search_jira_issues":
            return [{"key": "RHEL-300"}]
        if kwargs["issue_key"] == "RHEL-300":
            return {
                "fields": {
                    "comment": {"comments": [{"body": "Queued for triage as potential sibling of RHEL-2001"}]}
                }
            }
        return {"fields": {}}

    monkeypatch.setattr(rebase_consolidation, "run_tool", run_tool)
    siblings, _ = await find_triaged_rebase_siblings(
        jira_issue="RHEL-200",
        rebase_data=RebaseData(
            jira_issue="RHEL-200", package="bash", version="5.3", fix_version="rhel-10.3.z"
        ),
        available_tools=[],
    )
    assert siblings == []
