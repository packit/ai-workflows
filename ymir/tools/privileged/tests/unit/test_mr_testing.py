import pytest

from ymir.tools.privileged.mr_testing import (
    ExtractTfRequestFromJobTraceTool,
    FoldMrCommentBodyTool,
    FormatMrTestingCommentTool,
    ParseMrTestResultsTool,
    ParseNewaMrCommentTool,
    ParseOsciResultsCommentTool,
)

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
    assert result.unfixed_results_by_arch == {}
    assert result.report_portal_url is None

    # Dict keyed by architecture, directly usable as format_mr_testing_comment's
    # results_by_arch -- no reshaping needed.
    by_arch = result.results_by_arch
    assert by_arch["x86_64"]["plans"]["/plans/general"] == "✅ pass"
    assert by_arch["x86_64"]["artifacts_url"] == "https://artifacts.osci.redhat.com/testing-farm/aaa"
    assert by_arch["x86_64"]["request_id"] == "aaa"
    assert by_arch["x86_64"]["plans"]["/plans/upstream-tests"] == "✅ pass"
    assert by_arch["aarch64"]["plans"]["/plans/general"] == "❌ fail"
    assert by_arch["aarch64"]["artifacts_url"] == "https://artifacts.osci.redhat.com/testing-farm/ccc"


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
    by_arch = result.results_by_arch
    assert by_arch["x86_64"]["overall_status"] == "✅ passed"
    assert by_arch["x86_64"]["artifacts_url"] == "https://artifacts.osci.redhat.com/testing-farm/xyz"
    assert by_arch["x86_64"]["request_id"] == "xyz"


@pytest.mark.asyncio
async def test_parse_no_anewa_comment_found():
    comments = [
        {"id": 1, "author": "someone", "created_at": "2026-01-01T00:00:00Z", "body": "unrelated comment"},
    ]

    out = await ParseMrTestResultsTool().run(input={"comments": comments})
    result = out.result

    assert result.nvr is None
    assert result.results_by_arch == {}
    assert result.unfixed_results_by_arch == {}
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


@pytest.mark.asyncio
async def test_parse_note_id_never_falls_back_to_discussion_id():
    """update_merge_request_comment requires a numeric note ID; a GitLab
    discussion_id (a long hex string) is a different identifier it can't
    accept. A comment with only a discussion_id (as returned by
    get_authorized_comments_from_merge_request before note_id was added)
    must not produce a bogus note_id -- the caller should instead treat this
    as "no fold target" and post a new comment."""
    body = "**ANEWA:** Testing for `foo-1.0-1.el9` completed.\n**Build:** `foo-1.0-1.el9`\n"
    comments = [
        {
            "created_at": "2026-01-01T00:00:00Z",
            "body": body,
            "discussion_id": "a7913831544adb740d747ffc7a5650a147236d70",
        }
    ]

    out = await ParseMrTestResultsTool().run(input={"comments": comments})
    result = out.result

    assert result.note_id is None


@pytest.mark.asyncio
async def test_parse_note_id_prefers_real_note_id_over_discussion_id():
    body = "**ANEWA:** Testing for `foo-1.0-1.el9` completed.\n**Build:** `foo-1.0-1.el9`\n"
    comments = [
        {
            "created_at": "2026-01-01T00:00:00Z",
            "body": body,
            "discussion_id": "a7913831544adb740d747ffc7a5650a147236d70",
            "note_id": 999,
        }
    ]

    out = await ParseMrTestResultsTool().run(input={"comments": comments})
    result = out.result

    assert result.note_id == 999


@pytest.mark.asyncio
async def test_parse_rerun_ignores_previously_folded_analysis():
    """On a rerun, anewa's completed comment already contains this
    workflow's own prior fold (after FOLDED_RESULTS_MARKER) -- a formatted
    Testing Farm Results comment with its own "Results by Architecture"
    table and possibly an Untested Issues coverage table. Table parsing must
    stop at the marker so those tables aren't picked up as more anewa
    architecture rows."""
    body = (
        "**ANEWA:** Testing for `foo-1.0-1.el9` completed.\n"
        "**Build:** `foo-1.0-1.el9`\n\n"
        "| Architecture | /plans/general |\n"
        "|---|---|\n"
        "| x86_64 | [✅ pass](https://artifacts.osci.redhat.com/testing-farm/aaa) |\n"
        "\n---\n\n"
        "<!-- mr_testing:results -->\n"
        "## Testing Farm Results\n\n"
        "| Jira Issue | CVE | Test Status | Coverage |\n"
        "|---|---|---|---|\n"
        "| RHEL-12345 | CVE-2026-1 | PASSED | something |\n"
    )
    comments = [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": body}]

    out = await ParseMrTestResultsTool().run(input={"comments": comments})
    result = out.result

    assert set(result.results_by_arch.keys()) == {"x86_64"}
    assert "RHEL-12345" not in result.results_by_arch


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


@pytest.mark.asyncio
async def test_format_gating_results_markdown():
    out = await FormatMrTestingCommentTool().run(
        input={
            "action": "gating_results_markdown",
            "pipeline_id": "2860386105",
            "pipeline_url": "https://gitlab.com/redhat/rhel/rpms/resource-agents/-/pipelines/2860386105",
            "all_passed": False,
            "test_jobs": {
                "osci.tier0": {
                    "status": "✅",
                    "artifacts_url": "https://artifacts.osci.redhat.com/testing-farm/aaa",
                    "result_summary": "1 pass",
                },
                "osci.rpminspect": {
                    "status": "❌",
                    "artifacts_url": "https://artifacts.osci.redhat.com/testing-farm/bbb",
                    "result_summary": "1 fail",
                },
            },
        }
    )
    comment = out.result

    assert "## Gating Pipeline Results" in comment
    assert (
        "[2860386105](https://gitlab.com/redhat/rhel/rpms/resource-agents/-/pipelines/2860386105)" in comment
    )
    assert "One or more gating tests failed or errored" in comment
    assert "`osci.tier0`" in comment
    assert "`osci.rpminspect`" in comment
    assert ":x: (1 fail)" in comment


# ---------------------------------------------------------------------------
# ParseNewaMrCommentTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_newa_comment_completed_all_passed():
    body = (
        "✅ NEWA testing completed!\n\n"
        "**ReportPortal:** https://reportportal-rhel.apps.cluster/ui/#project/launches/all/123\n"
        "**Jenkins build:** https://jenkins.host/job/job-name/5/console\n"
    )
    comments = [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": body}]

    out = await ParseNewaMrCommentTool().run(input={"comments": comments})
    result = out.result

    assert result.found is True
    assert result.completed is True
    assert result.all_passed is True
    assert result.report_portal_url == "https://reportportal-rhel.apps.cluster/ui/#project/launches/all/123"
    assert result.jenkins_url == "https://jenkins.host/job/job-name/5/console"


@pytest.mark.asyncio
async def test_parse_newa_comment_started_not_completed():
    comments = [
        {
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "body": "NEWA testing started for this merge request.",
        }
    ]

    out = await ParseNewaMrCommentTool().run(input={"comments": comments})
    result = out.result

    assert result.found is True
    assert result.completed is False


@pytest.mark.asyncio
async def test_parse_newa_comment_not_found():
    comments = [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": "unrelated comment"}]

    out = await ParseNewaMrCommentTool().run(input={"comments": comments})
    result = out.result

    assert result.found is False
    assert result.completed is False
    assert result.all_passed is None


# ---------------------------------------------------------------------------
# ParseOsciResultsCommentTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_osci_results_comment_results_data_present():
    """centos-stream/rpms bot emits a <!--RESULTS_DATA--> JSON blob."""
    body = (
        "<!--RESULTS_COMMENT-->\n"
        "**Results for pipeline [123](https://gitlab.com/x/-/pipelines/123)**\n\n"
        "**All checks and builds passed! 🥳**\n"
        "<!--RESULTS_DATA\n"
        '{"jobs": {'
        '"osci.tier0": {"id": 1, "status": "success", "url": "https://gitlab.com/x/-/jobs/1", '
        '"stage": "test", "allow_failure": false, "pipeline_id": 123}, '
        '"generate_test_jobs": {"id": 2, "status": "success", "url": "https://gitlab.com/x/-/jobs/2", '
        '"stage": "test", "allow_failure": false, "pipeline_id": 123}, '
        '"report_results": {"id": 3, "status": "running", "url": "https://gitlab.com/x/-/jobs/3", '
        '"stage": "summary", "allow_failure": true, "pipeline_id": 123}'
        "}}\n-->"
    )
    comments = [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": body}]

    out = await ParseOsciResultsCommentTool().run(input={"comments": comments})
    result = out.result

    assert result.found is True
    assert result.pipeline_id == "123"
    assert result.banner_all_passed is True
    # "report_results" (stage=summary, still running) must not affect all_passed
    assert result.all_passed is True
    # "generate_test_jobs" (no dot in name) is scaffolding, not a real test job
    assert list(result.test_jobs.keys()) == ["osci.tier0"]


@pytest.mark.asyncio
async def test_parse_osci_results_comment_html_fallback_and_stale_banner():
    """rhel/rpms bot never emits RESULTS_DATA; parse the Check(s)/Build(s)/Test(s)
    <li> lists instead, and never trust the (possibly stale) banner."""
    body = (
        "<!--RESULTS_COMMENT-->\n"
        "**Results for pipeline [456](https://gitlab.com/x/-/pipelines/456)**\n\n"
        "**All checks and builds passed! 🥳**\n"
        "**Check(s)**\n<ul><li>✅ Tickets are approved? "
        "([logs](https://gitlab.com/x/-/jobs/10))</li></ul>\n"
        "**Build(s)**\n<ul><li>✅ build_rpm ([logs](https://gitlab.com/x/-/jobs/11))</li></ul>\n"
        "**Test(s)**\n<ul>"
        "<li>✅ osci.tier0 ([logs](https://gitlab.com/x/-/jobs/12))</li>"
        "<li>❌ osci.rpminspect ([logs](https://gitlab.com/x/-/jobs/13))</li>"
        "</ul>\n"
        "This merge request is **not ready to merge** because one or more jobs did not succeed."
    )
    comments = [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": body}]

    out = await ParseOsciResultsCommentTool().run(input={"comments": comments})
    result = out.result

    assert result.found is True
    assert result.pipeline_id == "456"
    assert result.banner_all_passed is True  # stale banner says passed
    assert result.all_passed is False  # osci.rpminspect actually failed
    assert set(result.test_jobs.keys()) == {"osci.tier0", "osci.rpminspect"}
    assert result.test_jobs["osci.rpminspect"]["status"] == "❌"
    assert result.test_jobs["osci.rpminspect"]["id"] == 13


@pytest.mark.asyncio
async def test_parse_osci_results_comment_not_found():
    comments = [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": "unrelated comment"}]

    out = await ParseOsciResultsCommentTool().run(input={"comments": comments})
    result = out.result

    assert result.found is False
    assert result.all_passed is None
    assert result.jobs == {}


# ---------------------------------------------------------------------------
# ExtractTfRequestFromJobTraceTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_tf_request_centos_stream_wording():
    trace = "log lines\nTesting Farm Result: https://artifacts.osci.redhat.com/testing-farm/abc-123\nmore log"

    out = await ExtractTfRequestFromJobTraceTool().run(input={"trace": trace})
    result = out.result

    assert result.request_id == "abc-123"
    assert result.artifacts_url == "https://artifacts.osci.redhat.com/testing-farm/abc-123"


@pytest.mark.asyncio
async def test_extract_tf_request_rhel_rpms_wording_and_http_scheme():
    trace = (
        'INFO: Status: success, message: "Test finished successfully", '
        "url: http://artifacts.osci.redhat.com/testing-farm/def-456\n"
        "TEST_RESULT_URL=http://artifacts.osci.redhat.com/testing-farm/def-456\n"
    )

    out = await ExtractTfRequestFromJobTraceTool().run(input={"trace": trace})
    result = out.result

    assert result.request_id == "def-456"
    # Always normalized to https regardless of the trace's own scheme
    assert result.artifacts_url == "https://artifacts.osci.redhat.com/testing-farm/def-456"


@pytest.mark.asyncio
async def test_extract_tf_request_not_found():
    out = await ExtractTfRequestFromJobTraceTool().run(input={"trace": "no testing farm link here"})
    result = out.result

    assert result.request_id is None
    assert result.artifacts_url is None


@pytest.mark.asyncio
async def test_extract_tf_request_no_trace():
    out = await ExtractTfRequestFromJobTraceTool().run(input={"trace": None})
    result = out.result

    assert result.request_id is None
    assert result.artifacts_url is None


# ---------------------------------------------------------------------------
# FoldMrCommentBodyTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fold_mr_comment_body_first_run():
    out = await FoldMrCommentBodyTool().run(
        input={
            "note_body": "**ANEWA:** done\n**Build:** `foo-1.0`",
            "formatted_markdown": "## Testing Farm Results",
        }
    )
    body = out.result

    assert body.startswith("**ANEWA:** done\n**Build:** `foo-1.0`\n\n---\n\n<!-- mr_testing:results -->\n")
    assert body.endswith("## Testing Farm Results")


@pytest.mark.asyncio
async def test_fold_mr_comment_body_rerun_replaces_prior_fold_idempotently():
    first = (
        await FoldMrCommentBodyTool().run(
            input={"note_body": "**ANEWA:** done", "formatted_markdown": "## Testing Farm Results\nold"}
        )
    ).result

    second = (
        await FoldMrCommentBodyTool().run(
            input={"note_body": first, "formatted_markdown": "## Testing Farm Results\nnew"}
        )
    ).result

    # Rerunning on the already-folded body produces exactly the same shape
    # as a first run -- no duplicated "---" separators or stacked folds.
    expected = (
        await FoldMrCommentBodyTool().run(
            input={"note_body": "**ANEWA:** done", "formatted_markdown": "## Testing Farm Results\nnew"}
        )
    ).result
    assert second == expected
    assert second.count("<!-- mr_testing:results -->") == 1
    assert "old" not in second
