from ymir.agents.rebase_consolidation import build_rebase_siblings_jql
from ymir.common.utils import extract_text_from_adf


def test_build_rebase_siblings_jql():
    jql = build_rebase_siblings_jql("RHEL-100", "dotnet10.0", "rhel-10.2")
    assert 'component = "dotnet10.0"' in jql
    assert 'fixVersion in ("rhel-10.2", "rhel-10.2.z")' in jql
    assert 'key != "RHEL-100"' in jql
    assert 'labels = "SecurityTracking"' in jql
    assert "labels not in" in jql
    assert '"ymir_triaged_rebase"' in jql  # Exclude to prevent circular consolidation
    assert '"ymir_triaged_not_affected"' in jql
    assert '"ymir_triaged_backport"' in jql
    assert '"ymir_triaged_rebuild"' in jql
    assert '"ymir_triaged_postponed"' in jql
    assert 'status in ("New", "Planning")' in jql


def test_build_rebase_siblings_jql_escapes_component_quotes():
    jql = build_rebase_siblings_jql("RHEL-100", 'comp"name', "rhel-9.8.z")
    assert r'component = "comp\"name"' in jql
    assert 'fixVersion in ("rhel-9.8", "rhel-9.8.z")' in jql


def test_build_rebase_siblings_jql_filters_modular_stream():
    """Modular issues must filter on Downstream Component Name to avoid mixing streams."""
    jql = build_rebase_siblings_jql(
        "RHEL-100", "postgis", "rhel-9.8", downstream_component="postgresql:16/postgis"
    )
    assert 'cf[10669] = "postgresql:16/postgis"' in jql
    assert 'component = "postgis"' in jql


def test_build_rebase_siblings_jql_no_filter_for_nonmodular():
    """Non-modular issues should not add a Downstream Component Name filter."""
    jql = build_rebase_siblings_jql("RHEL-100", "curl", "rhel-9.8", downstream_component="curl")
    assert "cf[10669]" not in jql


def test_build_rebase_siblings_jql_no_filter_when_none():
    """When downstream_component is None, no extra filter is added."""
    jql = build_rebase_siblings_jql("RHEL-100", "curl", "rhel-9.8", downstream_component=None)
    assert "cf[10669]" not in jql


def test_build_rebase_siblings_jql_excludes_correct_labels():
    """Verify that rebase consolidation excludes terminal triage labels to prevent circular consolidation."""
    jql = build_rebase_siblings_jql("RHEL-100", "python3.12", "rhel-9.8")
    # Should exclude issues already triaged (prevents circular consolidation)
    assert '"ymir_triaged_not_affected"' in jql
    assert '"ymir_triaged_backport"' in jql
    assert '"ymir_triaged_rebuild"' in jql
    assert '"ymir_triaged_rebase"' in jql  # Prevents circular consolidation
    assert '"ymir_triaged_postponed"' in jql


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
