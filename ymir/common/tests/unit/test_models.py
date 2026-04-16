from ymir.common.models import (
    AUTOMATED_RESOLUTION_NOT_SUPPORTED,
    TRIAGE_DISCLAIMER,
    BackportData,
    ClarificationNeededData,
    ErrorData,
    OpenEndedAnalysisData,
    RebaseData,
    Resolution,
    TriageOutputSchema,
)


def test_backport_formatting():
    data = BackportData(
        package="readline",
        patch_urls=["https://example.com/patch.patch"],
        justification="Fixes the bug in bind.c",
        jira_issue="RHEL-12345",
        cve_id="CVE-2024-1234",
        fix_version="rhel-10.0",
    )
    result = TriageOutputSchema(resolution=Resolution.BACKPORT, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: backport\n"
        "*Patch URL 1*: https://example.com/patch.patch\n"
        "*Justification*: Fixes the bug in bind.c\n"
        "*Fix Version*: rhel-10.0"
        "\n\n_Automated individual follow-up workflow for this "
        "resolution type is planned for Q2 2026. Stay tuned._"
        f"{TRIAGE_DISCLAIMER}"
    )


def test_backport_formatting_auto_chain():
    data = BackportData(
        package="readline",
        patch_urls=["https://example.com/patch.patch"],
        justification="Fixes the bug in bind.c",
        jira_issue="RHEL-12345",
        cve_id="CVE-2024-1234",
        fix_version="rhel-10.0",
    )
    result = TriageOutputSchema(resolution=Resolution.BACKPORT, data=data)

    comment = result.format_for_comment(auto_chain=True)
    assert "planned for Q2 2026" not in comment
    assert "*Resolution*: backport" in comment


def test_rebase_formatting():
    data = RebaseData(
        package="httpd",
        version="2.4.55",
        jira_issue="RHEL-67890",
        fix_version="rhel-9.5",
    )
    result = TriageOutputSchema(resolution=Resolution.REBASE, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: rebase\n"
        "*Package*: httpd\n"
        "*Version*: 2.4.55\n"
        "*Fix Version*: rhel-9.5"
        "\n\n_Automated individual follow-up workflow for this "
        "resolution type is planned for Q2 2026. Stay tuned._"
        f"{TRIAGE_DISCLAIMER}"
    )


def test_rebase_formatting_auto_chain():
    data = RebaseData(
        package="httpd",
        version="2.4.55",
        jira_issue="RHEL-67890",
        fix_version="rhel-9.5",
    )
    result = TriageOutputSchema(resolution=Resolution.REBASE, data=data)

    comment = result.format_for_comment(auto_chain=True)
    assert "planned for Q2 2026" not in comment
    assert "*Resolution*: rebase" in comment


def test_clarification_needed_formatting():
    data = ClarificationNeededData(
        findings="Found a potential buffer overflow",
        additional_info_needed="Need upstream patch URL",
        jira_issue="RHEL-11111",
    )
    result = TriageOutputSchema(resolution=Resolution.CLARIFICATION_NEEDED, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: clarification-needed\n"
        "*Findings*: Found a potential buffer overflow\n"
        "*Additional info needed*: Need upstream patch URL"
        f"{TRIAGE_DISCLAIMER}"
    )


def test_open_ended_analysis_formatting():
    data = OpenEndedAnalysisData(
        summary="This is a feature request, not a bug",
        recommendation="No action needed — feature requests are not appropriate for bugfix updates in RHEL.",
        jira_issue="RHEL-22222",
    )
    result = TriageOutputSchema(resolution=Resolution.OPEN_ENDED_ANALYSIS, data=data)

    assert result.format_for_comment() == (
        "*Summary*: This is a feature request, not a bug\n"
        "*Recommendation*: No action needed — feature requests are not "
        "appropriate for bugfix updates in RHEL."
        f"{AUTOMATED_RESOLUTION_NOT_SUPPORTED}"
        f"{TRIAGE_DISCLAIMER}"
    )


def test_error_formatting():
    data = ErrorData(details="Package 'invalid-pkg' not found in repository", jira_issue="RHEL-33333")
    result = TriageOutputSchema(resolution=Resolution.ERROR, data=data)

    assert result.format_for_comment() == (
        f"*Resolution*: error\n*Details*: Package 'invalid-pkg' not found in repository{TRIAGE_DISCLAIMER}"
    )
