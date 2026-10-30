import pytest

from ymir.tools.privileged.mr_testing import FormatMrTestingCommentTool, ParseMrTestResultsTool

# ---------------------------------------------------------------------------
# ParseMrTestResultsTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_modern_per_plan_artifact_links():
    """Modern anewa comments link each plan cell to its own artifacts URL."""
    body = (
        "**ANEWA:** Testing for `perl-IO-Compress` completed.\n"
        "**Build:** `perl-IO-Compress-2.102-4.el9_8.1`\n\n"
        "| Architecture | /plans/general | /plans/upstream-tests |\n"
        "|---|---|---|\n"
        "| x86_64 | [✅ pass](https://artifacts.osci.redhat.com/testing-farm/aaa) | "
        "[✅ pass](https://artifacts.osci.redhat.com/testing-farm/bbb) |\n"
        "| aarch64 | [❌ fail](https://artifacts.osci.redhat.com/testing-farm/ccc) | "
        "[✅ pass](https://artifacts.osci.redhat.com/testing-farm/ddd) |\n"
    )
    comments = [
        {"id": 42, "author": "anewa", "created_at": "2026-01-01T00:00:00Z", "body": body},
    ]

    out = await ParseMrTestResultsTool().run(input={"comments": comments})
    result = out.result

    assert result.nvr == "perl-IO-Compress-2.102-4.el9_8.1"
    assert result.note_id == 42
    assert result.note_body == body
    assert result.unfixed_results_by_arch == []
    assert result.report_portal_url is None

    by_arch = {row["architecture"]: row for row in result.results_by_arch}
    assert by_arch["x86_64"]["plan_/plans/general"] == "✅ pass"
    assert (
        by_arch["x86_64"]["plan_/plans/general_url"] == "https://artifacts.osci.redhat.com/testing-farm/aaa"
    )
    assert by_arch["x86_64"]["plan_/plans/upstream-tests"] == "✅ pass"
    assert by_arch["aarch64"]["plan_/plans/general"] == "❌ fail"
    assert (
        by_arch["aarch64"]["plan_/plans/general_url"] == "https://artifacts.osci.redhat.com/testing-farm/ccc"
    )


@pytest.mark.asyncio
async def test_parse_older_shared_artifact_link_format():
    """Older anewa comments have a single shared Artifacts column per row."""
    body = (
        "**ANEWA:** Testing for `foo-1.0-1.el9` completed.\n"
        "**Build:** `foo-1.0-1.el9`\n\n"
        "| Architecture | Result | Artifacts |\n"
        "|---|---|---|\n"
        "| x86_64 | ✅ passed | [artifacts](https://artifacts.osci.redhat.com/testing-farm/xyz) |\n"
    )
    comments = [
        {"id": "note-1", "author": "anewa", "created_at": "2026-01-01T00:00:00Z", "message": body},
    ]

    out = await ParseMrTestResultsTool().run(input={"comments": comments})
    result = out.result

    assert result.nvr == "foo-1.0-1.el9"
    assert result.note_id == "note-1"
    by_arch = {row["architecture"]: row for row in result.results_by_arch}
    assert by_arch["x86_64"]["overall_status"] == "✅ passed"
    assert by_arch["x86_64"]["artifacts_url"] == "https://artifacts.osci.redhat.com/testing-farm/xyz"


@pytest.mark.asyncio
async def test_parse_no_anewa_comment_found():
    comments = [
        {"id": 1, "author": "someone", "created_at": "2026-01-01T00:00:00Z", "body": "unrelated comment"},
    ]

    out = await ParseMrTestResultsTool().run(input={"comments": comments})
    result = out.result

    assert result.nvr is None
    assert result.results_by_arch == []
    assert result.unfixed_results_by_arch == []
    assert result.note_id is None
    assert result.note_body is None
    assert result.report_portal_url is None


@pytest.mark.asyncio
async def test_parse_report_portal_url():
    body = (
        "**ANEWA:** Testing for `foo-1.0-1.el9` completed.\n"
        "**Build:** `foo-1.0-1.el9`\n"
        "**Report Portal:** https://reportportal.example.com/run/123\n\n"
        "| Architecture | Result |\n|---|---|\n| x86_64 | ✅ passed |\n"
    )
    comments = [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": body}]

    out = await ParseMrTestResultsTool().run(input={"comments": comments})
    result = out.result

    assert result.report_portal_url == "https://reportportal.example.com/run/123"


# ---------------------------------------------------------------------------
# FormatMrTestingCommentTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_format_testing_farm_markdown_gitlab_shortcodes():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "testing_farm_markdown",
            "all_passed": True,
            "architectures": ["x86_64", "aarch64"],
            "results_by_arch": {
                "x86_64": {
                    "overall_status": "✅ passed (3 pass)",
                    "plans": {"/plans/general": "✅ pass (3 pass)"},
                    "artifacts_url": "https://artifacts.osci.redhat.com/testing-farm/aaa",
                },
                "aarch64": {
                    "overall_status": "✅ passed (3 pass)",
                    "plans": {"/plans/general": "✅ pass (3 pass)"},
                    "artifacts_url": "https://artifacts.osci.redhat.com/testing-farm/bbb",
                },
            },
            "plans": ["/plans/general"],
        }
    )
    comment = out.result

    assert "## Testing Farm Results" in comment
    # GitLab target (default) renders shortcode emoji, not unicode
    assert ":white_check_mark:" in comment
    assert "✅" not in comment
    assert "passed on all 2 architectures (x86_64, aarch64)" in comment
    assert "Results by Architecture" in comment


@pytest.mark.asyncio
async def test_format_testing_farm_markdown_jira_target_uses_unicode():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "testing_farm_markdown",
            "all_passed": False,
            "architectures": ["x86_64"],
            "results_by_arch": {
                "x86_64": {
                    "overall_status": "❌ failed (1 fail, 2 pass)",
                    "plans": {"/plans/general": "❌ fail"},
                },
            },
            "plans": ["/plans/general"],
            "target": "jira",
        }
    )
    comment = out.result

    assert "Some tests failed or errored" in comment
    assert "⚠️" in comment
    assert ":warning:" not in comment


@pytest.mark.asyncio
async def test_format_cve_verification_markdown():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "cve_verification_markdown",
            "cve_id": "CVE-2026-1234",
            "jira_key": "RHEL-12345",
            "architectures": ["x86_64", "aarch64"],
            "results_by_arch": {
                "x86_64": {"cve_CVE-2026-1234_status": "✅ PASSED"},
                "aarch64": {"cve_CVE-2026-1234_status": "✅ PASSED"},
            },
            "component": "ldns",
            "plan_name": "/plans/errata",
            "test_path": "/Security/CVE-2026-1234",
        }
    )
    comment = out.result

    assert "## CVE-2026-1234 Test Verification" in comment
    assert "RHEL-12345" in comment
    assert "/Security/CVE-2026-1234" in comment
    assert "verified by automated testing across all supported architectures" in comment


@pytest.mark.asyncio
async def test_format_cve_verification_markdown_unfixed_fixed():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "cve_verification_markdown",
            "cve_id": "CVE-2026-1234",
            "jira_key": "RHEL-12345",
            "architectures": ["x86_64"],
            "results_by_arch": {"x86_64": {"cve_CVE-2026-1234_status": "✅ pass"}},
            "unfixed_results_by_arch": {"x86_64": {"cve_CVE-2026-1234_status": "❌ fail"}},
            "component": "ldns",
            "plan_name": "/plans/errata",
            "test_path": "/Security/CVE-2026-1234",
        }
    )
    comment = out.result

    assert "Unfixed Build" in comment
    assert "Fixed Build" in comment
    assert "Fix Verification" in comment
    assert "fails on the unfixed build and passes on the fixed build" in comment


@pytest.mark.asyncio
async def test_format_jira_issue_coverage_found():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "jira_issue_coverage",
            "test_found": True,
            "architectures": ["x86_64"],
            "results_by_arch": {"x86_64": {"cve_CVE-2026-1234_status": "✅ PASSED"}},
            "cve_id": "CVE-2026-1234",
        }
    )
    comment = out.result

    assert "The test covering this issue was executed:" in comment
    assert "✅ PASSED" in comment


@pytest.mark.asyncio
async def test_format_jira_issue_coverage_not_found():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "jira_issue_coverage",
            "test_found": False,
            "reason": "No test link on the issue",
        }
    )
    comment = out.result

    assert "No test was found covering this issue" in comment
    assert "No test link on the issue" in comment


@pytest.mark.asyncio
async def test_format_jira_summary_comment():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "jira_summary_comment",
            "jira_key": "RHEL-12345",
            "mr_url": "https://gitlab.com/redhat/rhel/rpms/foo/-/merge_requests/1",
            "test_section": "✅ **All tests passed** across all architectures",
            "issue_coverage": "The test covering this issue was executed:\n\n| Architecture | Test Result |",
            "report_portal_url": "https://reportportal.example.com/run/123",
        }
    )
    comment = out.result

    assert "## Testing Results" in comment
    assert "All tests passed" in comment
    assert "**Report Portal:** [https://reportportal.example.com/run/123]" in comment
    assert "[merge request](https://gitlab.com/redhat/rhel/rpms/foo/-/merge_requests/1)" in comment


@pytest.mark.asyncio
async def test_format_untested_issues_markdown():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "untested_issues_markdown",
            "untested_issues": [
                {
                    "jira_key": "RHEL-12345",
                    "cve_id": "CVE-2026-1234",
                    "test_coverage": "Manual",
                    "reason": "No test link on the issue",
                },
                {
                    "jira_key": "RHEL-12346",
                    "test_coverage": None,
                    "reason": "Test link exists but no matching testcase found",
                },
            ],
        }
    )
    comment = out.result

    assert "## Untested Issues" in comment
    assert "RHEL-12345" in comment
    assert "CVE-2026-1234" in comment
    assert "RHEL-12346" in comment
    assert "(not set)" in comment
    assert "—" in comment  # dash placeholder for missing CVE


@pytest.mark.asyncio
async def test_format_test_failures_markdown():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "test_failures_markdown",
            "failures": [
                {
                    "test_name": "/Sanity/smoke",
                    "architectures": ["x86_64", "aarch64"],
                    "result": "FAILED",
                    "phase": "Test",
                    "what_happened": "Smoke test failed during execution",
                    "key_error": "AssertionError: expected 0, got 1",
                    "likely_cause": "Actual regression",
                    "assessment": "Real failure",
                    "journal_urls": {
                        "x86_64": "https://artifacts.osci.redhat.com/testing-farm/aaa/journal.txt"
                    },
                },
            ],
        }
    )
    comment = out.result

    assert "## Test Failure Analysis" in comment
    assert "/Sanity/smoke" in comment
    assert "x86_64, aarch64" in comment
    assert "AssertionError: expected 0, got 1" in comment
    assert "Real failure" in comment
    assert "[x86_64 test log]" in comment
    assert "This is AI analysis of the test failure" in comment


@pytest.mark.asyncio
async def test_format_missing_required_field_raises():
    from beeai_framework.tools import ToolError

    with pytest.raises(ToolError):
        await FormatMrTestingCommentTool().run(input={"action": "jira_summary_comment"})
