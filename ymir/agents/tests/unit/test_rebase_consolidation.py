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


def test_build_rebase_siblings_jql_excludes_correct_labels():
    """Verify that JQL excludes non-retriable states but includes retriable FAILED labels.

    Per jira_label_workflow_routing.md:
    - ERRORED labels (triage/backport/rebase_errored) block retry → exclude
    - FAILED labels (backport/rebase_failed) may auto-retry → include (don't exclude)

    This prevents missing pending siblings when there are >50 total candidates.
    """
    jql = build_rebase_siblings_jql("RHEL-100", "python3.12", "rhel-9.8")

    # Triage decisions (non-retriable)
    assert '"ymir_triaged_not_affected"' in jql
    assert '"ymir_triaged_backport"' in jql
    assert '"ymir_triaged_rebuild"' in jql
    assert '"ymir_triaged_rebase"' in jql
    assert '"ymir_triaged_postponed"' in jql

    # Completion labels (non-retriable)
    assert '"ymir_backported"' in jql
    assert '"ymir_rebased"' in jql
    assert '"ymir_rebuilt"' in jql

    # ERRORED labels (block retry, must exclude)
    assert '"ymir_triage_errored"' in jql
    assert '"ymir_backport_errored"' in jql
    assert '"ymir_rebase_errored"' in jql
    assert '"ymir_rebuild_errored"' in jql

    # FAILED labels (may auto-retry, must NOT exclude)
    assert '"ymir_backport_failed"' not in jql
    assert '"ymir_rebase_failed"' not in jql
    assert '"ymir_rebuild_failed"' not in jql

    # ymir_rebase_sibling must NOT be excluded - it's a queueing state, not a terminal state
    # Excluding it would break check_and_queue_primary_if_ready() which needs to find
    # queued-but-not-started siblings
    assert '"ymir_rebase_sibling"' not in jql


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


class TestTerminalLabels:
    """Behavioral tests for terminal label handling in sibling consolidation.

    These tests verify that the production code actually excludes all terminal states,
    preventing bugs like RHEL-248139 where primaries got stuck waiting for siblings
    that had already finished with ymir_backported or ymir_backport_errored.
    """

    def test_jql_excludes_all_triage_decision_labels(self):
        """JQL must exclude all triage decision labels to avoid re-queueing decided siblings."""
        from ymir.common.constants import JiraLabels

        jql = build_rebase_siblings_jql("RHEL-100", "postgresql", "rhel-9.8")

        # Verify each triage decision label appears in the JQL exclusion
        for label in [
            JiraLabels.TRIAGED_REBASE.value,
            JiraLabels.TRIAGED_BACKPORT.value,
            JiraLabels.TRIAGED_REBUILD.value,
            JiraLabels.TRIAGED_NOT_AFFECTED.value,
            JiraLabels.TRIAGED_POSTPONED.value,
        ]:
            assert f'"{label}"' in jql, f"JQL must exclude {label} but it's missing from: {jql}"

    def test_jql_excludes_all_completion_labels(self):
        """JQL must exclude completion labels or primaries wait forever for completed siblings.

        Regression test for RHEL-248139 where ymir_backported was not excluded.
        """
        from ymir.common.constants import JiraLabels

        jql = build_rebase_siblings_jql("RHEL-100", "postgresql", "rhel-9.8")

        # These were the missing labels that caused RHEL-248139
        for label in [
            JiraLabels.BACKPORTED.value,
            JiraLabels.REBASED.value,
            JiraLabels.REBUILT.value,
        ]:
            assert f'"{label}"' in jql, f"JQL must exclude {label} but it's missing from: {jql}"

    def test_jql_excludes_errored_labels(self):
        """JQL must exclude ERRORED labels which block retry.

        Per jira_label_workflow_routing.md: ERRORED labels (triage/backport/rebase_errored)
        block retry and need human attention, so they're terminal for sibling queueing.
        """
        from ymir.common.constants import JiraLabels

        jql = build_rebase_siblings_jql("RHEL-100", "postgresql", "rhel-9.8")

        # ERRORED labels block retry → must exclude
        for label in [
            JiraLabels.TRIAGE_ERRORED.value,
            JiraLabels.BACKPORT_ERRORED.value,
            JiraLabels.REBASE_ERRORED.value,
            JiraLabels.REBUILD_ERRORED.value,
        ]:
            assert f'"{label}"' in jql, (
                f"JQL must exclude {label} (blocks retry) but it's missing from: {jql}"
            )

    def test_jql_includes_failed_labels(self):
        """JQL must NOT exclude FAILED labels which may auto-retry.

        Per jira_label_workflow_routing.md: FAILED labels (backport/rebase_failed)
        "May auto-retry", so excluding them breaks the retry mechanism where a new
        sibling triggers re-queueing of failed issues.
        """
        from ymir.common.constants import JiraLabels

        jql = build_rebase_siblings_jql("RHEL-100", "postgresql", "rhel-9.8")

        # FAILED labels may auto-retry → must NOT exclude
        for label in [
            JiraLabels.BACKPORT_FAILED.value,
            JiraLabels.REBASE_FAILED.value,
            JiraLabels.REBUILD_FAILED.value,
        ]:
            assert f'"{label}"' not in jql, (
                f"JQL must NOT exclude {label} (may auto-retry) but it's excluded in: {jql}"
            )

    def test_jql_does_not_exclude_sibling_marker(self):
        """JQL must NOT exclude ymir_rebase_sibling - it's a queueing state, not terminal.

        Regression test: check_and_queue_primary_if_ready() needs to find queued siblings
        that haven't started triage yet (have ymir_rebase_sibling label). If we excluded
        this label, the primary would be released early while siblings are still pending.

        queue_siblings_for_triage() handles the re-queueing check in its defensive filter.
        """
        from ymir.common.constants import JiraLabels

        jql = build_rebase_siblings_jql("RHEL-100", "postgresql", "rhel-9.8")

        assert f'"{JiraLabels.REBASE_SIBLING.value}"' not in jql, (
            f"JQL must NOT exclude {JiraLabels.REBASE_SIBLING.value} (queueing state, not terminal)"
        )

    def test_jql_exclusion_applies_before_50_result_limit(self):
        """Terminal labels must be excluded in JQL, not post-query, to avoid missing pending siblings.

        If there are 60 siblings where 40 have terminal labels and 20 are pending:
        - Correct: JQL excludes 40 terminal, returns 20 pending
        - Bug: JQL returns first 50 (35 terminal + 15 pending), post-filter → miss 5 pending

        This test verifies the exclusion is in the JQL string (server-side filtering).
        """
        from ymir.common.constants import JiraLabels

        jql = build_rebase_siblings_jql("RHEL-100", "postgresql", "rhel-9.8")

        # Critical: the exclusion MUST be in the JQL query string itself
        assert "labels not in" in jql, "JQL must have 'labels not in' clause for server-side filtering"

        # Spot-check a few terminal labels to ensure they're in the JQL, not filtered post-query
        critical_labels = [
            JiraLabels.BACKPORTED.value,  # Caused RHEL-248139
            JiraLabels.BACKPORT_ERRORED.value,  # ERRORED blocks retry, must exclude
            JiraLabels.TRIAGE_ERRORED.value,  # ERRORED blocks retry, must exclude
        ]
        for label in critical_labels:
            assert f'"{label}"' in jql, (
                f"Critical terminal label {label} must be in JQL for server-side filtering"
            )

        # FAILED labels must NOT be in JQL (they're retriable)
        retriable_labels = [
            JiraLabels.BACKPORT_FAILED.value,
            JiraLabels.REBASE_FAILED.value,
        ]
        for label in retriable_labels:
            assert f'"{label}"' not in jql, f"Retriable label {label} must NOT be excluded in JQL"

    def test_queued_sibling_blocks_primary(self):
        """Regression: Queued siblings with ymir_rebase_sibling must be found as pending.

        Before fix: build_rebase_siblings_jql() excluded ymir_rebase_sibling, then
        check_and_queue_primary_if_ready() added AND labels = "ymir_rebase_sibling",
        resulting in zero matches. Primary was released while queued siblings were pending.

        After fix: ymir_rebase_sibling is NOT excluded in JQL, so the pending query
        correctly finds queued-but-not-started siblings.
        """

        # Simulate the pending-sibling query in check_and_queue_primary_if_ready()
        jql = build_rebase_siblings_jql("RHEL-100", "postgresql", "rhel-9.8")

        # The query should be able to find siblings with ymir_rebase_sibling
        # This is the key fix: if ymir_rebase_sibling were excluded from JQL,
        # then check_and_queue_primary_if_ready() adding:
        #   AND (labels = "ymir_rebase_sibling" OR labels = "ymir_triage_in_progress")
        # would return zero results (contradictory query: exclude X AND require X)

        # The key assertion: ymir_rebase_sibling must NOT appear in the exclusion list
        assert '"ymir_rebase_sibling"' not in jql, (
            "ymir_rebase_sibling in exclusion list would make pending query contradictory"
        )
