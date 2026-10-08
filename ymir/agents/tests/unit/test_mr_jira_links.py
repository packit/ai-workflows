"""Unit tests for MR Jira link helpers and issue extraction."""

import re
from html.parser import HTMLParser

import pytest

from ymir.agents.constants import (
    format_jira_links_for_mr,
    mr_description_footer,
    strip_resolves_from_mr_text,
)
from ymir.agents.merge_request_agent import extract_jira_issue
from ymir.agents.mr_consolidation_agent import _build_consolidated_description


def test_format_jira_links_single():
    assert format_jira_links_for_mr("RHEL-12345") == (
        "Jira: [RHEL-12345](https://redhat.atlassian.net/browse/RHEL-12345)\n"
    )


def test_format_jira_links_multiple():
    result = format_jira_links_for_mr(["RHEL-1", "RHEL-2"])
    assert result.startswith("### Resolved Jira Issues\n")
    assert "- [RHEL-1](https://redhat.atlassian.net/browse/RHEL-1)" in result
    assert "- [RHEL-2](https://redhat.atlassian.net/browse/RHEL-2)" in result
    assert "Resolves:" not in result


def test_format_jira_links_empty():
    assert format_jira_links_for_mr([]) == ""
    assert format_jira_links_for_mr(None) == ""
    assert format_jira_links_for_mr([None, "", "RHEL-1"]) == (  # type: ignore[list-item]
        "Jira: [RHEL-1](https://redhat.atlassian.net/browse/RHEL-1)\n"
    )


def test_strip_resolves_from_mr_text():
    text = (
        "Backport fix.\n\n"
        "Resolves: RHEL-12345\n\n"
        "More text.\n"
        "Related: RHEL-999\n"
        "Jira: [RHEL-111](https://redhat.atlassian.net/browse/RHEL-111)\n"
    )
    assert strip_resolves_from_mr_text(text) == "Backport fix.\n\nMore text."


def test_strip_resolves_with_list_markers():
    text = (
        "Changelog-style lines.\n"
        "- Resolves: RHEL-123\n"
        "* Related: RHEL-1\n"
        "  - Jira: [RHEL-2](https://redhat.atlassian.net/browse/RHEL-2)\n"
        "Keep this.\n"
    )
    assert strip_resolves_from_mr_text(text) == "Changelog-style lines.\nKeep this."


def test_strip_resolves_preserves_other_content():
    text = "Description only.\nNo footer."
    assert strip_resolves_from_mr_text(text) == text


def test_extract_jira_issue_from_jira_line():
    desc = "Fix something.\n\nJira: [RHEL-154342](https://redhat.atlassian.net/browse/RHEL-154342)\n"
    assert extract_jira_issue(desc) == "RHEL-154342"


def test_extract_jira_issue_from_resolves_legacy():
    assert extract_jira_issue("Resolves: RHEL-100\n") == "RHEL-100"


def test_extract_jira_issue_prefers_resolved_section_over_embedded_jira():
    desc = (
        "### Resolved Jira Issues\n\n"
        "- [RHEL-159051](https://redhat.atlassian.net/browse/RHEL-159051)\n"
        "- [RHEL-159075](https://redhat.atlassian.net/browse/RHEL-159075)\n\n"
        "### Source Merge Requests\n\n"
        "<details><summary>Original description</summary>\n\n"
        "Jira: [RHEL-11111](https://redhat.atlassian.net/browse/RHEL-11111)\n"
        "</details>\n"
    )
    assert extract_jira_issue(desc) == "RHEL-159051"


def test_extract_jira_issue_ignores_resolved_section_inside_details():
    desc = (
        "Jira: [RHEL-22222](https://redhat.atlassian.net/browse/RHEL-22222)\n\n"
        "<details><summary>Original description</summary>\n\n"
        "### Resolved Jira Issues\n\n"
        "- [RHEL-11111](https://redhat.atlassian.net/browse/RHEL-11111)\n"
        "</details>\n"
    )
    assert extract_jira_issue(desc) == "RHEL-22222"


def test_extract_jira_issue_ignores_details_without_section():
    desc = (
        "Top-level fix.\n\n"
        "<details><summary>Original description</summary>\n\n"
        "Jira: [RHEL-11111](https://redhat.atlassian.net/browse/RHEL-11111)\n"
        "</details>\n\n"
        "Jira: [RHEL-22222](https://redhat.atlassian.net/browse/RHEL-22222)\n"
    )
    assert extract_jira_issue(desc) == "RHEL-22222"


def test_extract_jira_issue_missing():
    with pytest.raises(RuntimeError, match="Failed to extract Jira issue"):
        extract_jira_issue("No jira here")


def test_consolidated_description_strips_resolves_from_embedded_sources():
    desc = _build_consolidated_description(
        mr_titles=["Fix CVE"],
        mr_descriptions=[
            "Backport fix.\n\n"
            "Resolves: RHEL-159051\n"
            "- Resolves: RHEL-159051\n"
            "Jira: [RHEL-159051](https://redhat.atlassian.net/browse/RHEL-159051)\n"
        ],
        mr_urls=["https://gitlab.example/mr/1"],
        jira_issues=["RHEL-159051", "RHEL-159075"],
        package="gnutls",
    )
    assert "Resolves:" not in desc
    assert "Related:" not in desc
    # Top-level section keeps browse links; embedded Jira: footers are stripped
    assert desc.count("Jira:") == 0
    assert "[RHEL-159051](https://redhat.atlassian.net/browse/RHEL-159051)" in desc
    assert "[RHEL-159075](https://redhat.atlassian.net/browse/RHEL-159075)" in desc
    assert "Backport fix." in desc


class DescriptionParser(HTMLParser):
    """Track which trace links require expanding original descriptions."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            assert "open" not in dict(attrs)
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "details":
            self.depth -= 1
            assert self.depth >= 0

    def handle_data(self, data):
        for url in re.findall(r"https://trace\.example/[^\s)]+", data):
            self.links[url] = self.depth


@pytest.mark.parametrize("has_rebuild", [False, True])
def test_consolidated_description_preserves_nested_trace_links(monkeypatch, has_rebuild):
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example")
    source = (
        "<details>\n<summary>Triage Details</summary>\n\nReasoning\n\n</details>\n\n"
        "<details>\n<summary>Backporting steps</summary>\n\nApplied fix\n\n</details>\n\n"
        + mr_description_footer("bash", "RHEL-1")
    )
    earlier_trace = "https://trace.example/#/trace/_/" + "a" * 32
    current_trace = "https://trace.example/#/trace/_/" + "b" * 32
    earlier = _build_consolidated_description(
        ["Original backport"],
        [source],
        ["https://gitlab.example/mr/1"],
        ["RHEL-1"],
        "bash",
        has_rebuild=has_rebuild,
        trace_url=earlier_trace,
    )
    description = _build_consolidated_description(
        ["Earlier consolidation", "Another backport"],
        [earlier, mr_description_footer("bash", "RHEL-2")],
        ["https://gitlab.example/mr/2", "https://gitlab.example/mr/3"],
        ["RHEL-1", "RHEL-2"],
        "bash",
        trace_url=current_trace,
    )

    assert strip_resolves_from_mr_text(earlier) in description
    assert strip_resolves_from_mr_text(source) in description
    parser = DescriptionParser()
    parser.feed(description)
    assert parser.depth == 0
    assert parser.links == {
        current_trace: 0,
        earlier_trace: 1,
        "https://trace.example/#/issues/RHEL-1": 2,
        "https://trace.example/#/issues/RHEL-2": 1,
    }
    assert extract_jira_issue(description) == "RHEL-1"


def test_consolidated_description_links_run_without_jira_footers():
    trace_url = "https://trace.example/#/trace/_/" + "a" * 32
    description = _build_consolidated_description(
        ["A", "B"],
        ["First fix", "Second fix"],
        [],
        [],
        "bash",
        trace_url=trace_url,
    )

    assert f"[View this run's trace]({trace_url})" in description
    assert "Resolved Jira Issues" not in description
    assert "First fix" in description
    assert "Second fix" in description
