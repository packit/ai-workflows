from models import (
    TriageOutputSchema,
    BackportData,
    RebaseData,
    ClarificationNeededData,
    NoActionData,
    ErrorData,
    Resolution,
)


def test_backport_formatting():
    data = BackportData(
        package="readline",
        patch_urls=["https://example.com/patch.patch"],
        justification="Fixes the bug in bind.c",
        jira_issue="RHEL-12345",
        cve_id="CVE-2024-1234",
        fix_version="rhel-10.0"
    )
    result = TriageOutputSchema(resolution=Resolution.BACKPORT, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: backport\n"
        "*Patch URL 1*: https://example.com/patch.patch\n"
        "*Justification*: Fixes the bug in bind.c\n"
        "*Fix Version*: rhel-10.0"
    )


def test_rebase_formatting():
    data = RebaseData(
        package="httpd",
        version="2.4.55",
        jira_issue="RHEL-67890",
        fix_version="rhel-9.5"
    )
    result = TriageOutputSchema(resolution=Resolution.REBASE, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: rebase\n"
        "*Package*: httpd\n"
        "*Version*: 2.4.55\n"
        "*Fix Version*: rhel-9.5"
    )


def test_clarification_needed_formatting():
    data = ClarificationNeededData(
        findings="Found a potential buffer overflow",
        additional_info_needed="Need upstream patch URL",
        jira_issue="RHEL-11111"
    )
    result = TriageOutputSchema(resolution=Resolution.CLARIFICATION_NEEDED, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: clarification-needed\n"
        "*Findings*: Found a potential buffer overflow\n"
        "*Additional info needed*: Need upstream patch URL"
    )


def test_no_action_formatting():
    data = NoActionData(
        reasoning="This is a feature request, not a bug",
        jira_issue="RHEL-22222"
    )
    result = TriageOutputSchema(resolution=Resolution.NO_ACTION, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: no-action\n"
        "*Reasoning*: This is a feature request, not a bug"
    )


def test_error_formatting():
    data = ErrorData(
        details="Package 'invalid-pkg' not found in repository",
        jira_issue="RHEL-33333"
    )
    result = TriageOutputSchema(resolution=Resolution.ERROR, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: error\n"
        "*Details*: Package 'invalid-pkg' not found in repository"
    )
