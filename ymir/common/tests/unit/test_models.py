from ymir.common.models import (
    AUTOMATED_RESOLUTION_NOT_SUPPORTED,
    TRIAGE_DISCLAIMER,
    ApplicabilityResult,
    BackportData,
    ClarificationNeededData,
    ConsolidatedIssue,
    ErrorData,
    NotAffectedData,
    OpenEndedAnalysisData,
    PostponedData,
    RebaseData,
    RebuildData,
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


def test_postponed_formatting_multiple_issues():
    data = PostponedData(
        summary="Y-stream CVE (CVE-2025-12345): waiting for at least one Z-stream clone to ship",
        pending_issues=["RHEL-111", "RHEL-222"],
        jira_issue="RHEL-99999",
    )
    result = TriageOutputSchema(resolution=Resolution.POSTPONED, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: postponed\n"
        "*Summary*: Y-stream CVE (CVE-2025-12345): "
        "waiting for at least one Z-stream clone to ship\n"
        "*Waiting for at least one of*:\n"
        "* RHEL-111\n"
        "* RHEL-222"
        f"{TRIAGE_DISCLAIMER}"
    )


def test_postponed_formatting_single_issue():
    data = PostponedData(
        summary="Rebuild waiting for dependency to ship",
        pending_issues=["RHEL-333"],
        jira_issue="RHEL-99999",
    )
    result = TriageOutputSchema(resolution=Resolution.POSTPONED, data=data)

    assert result.format_for_comment() == (
        "*Resolution*: postponed\n"
        "*Summary*: Rebuild waiting for dependency to ship\n"
        "*Waiting for*:\n"
        "* RHEL-333"
        f"{TRIAGE_DISCLAIMER}"
    )


def test_postponed_rebuild_with_extra_fields():
    data = PostponedData(
        summary="Rebuild of butane waiting for RHEL-67890 (golang) to ship",
        pending_issues=["RHEL-67890"],
        jira_issue="RHEL-12345",
        package="butane",
        fix_version="rhel-10.1",
        cve_id="CVE-2026-99999",
        dependency_issue="RHEL-67890",
        dependency_component="golang",
    )
    assert data.package == "butane"
    assert data.dependency_issue == "RHEL-67890"
    assert data.dependency_component == "golang"
    assert data.cve_id == "CVE-2026-99999"
    assert data.fix_version == "rhel-10.1"

    result = TriageOutputSchema(resolution=Resolution.POSTPONED, data=data)
    comment = result.format_for_comment()
    assert "*Summary*: Rebuild of butane" in comment
    assert "RHEL-67890" in comment


def test_postponed_without_rebuild_fields():
    """Y-stream postponement — no rebuild-specific fields."""
    data = PostponedData(
        summary="CVE waiting for Z-stream clone to ship",
        pending_issues=["RHEL-111"],
        jira_issue="RHEL-99999",
    )
    assert data.package is None
    assert data.fix_version is None
    assert data.cve_id is None
    assert data.dependency_issue is None
    assert data.dependency_component is None

    result = TriageOutputSchema(resolution=Resolution.POSTPONED, data=data)
    comment = result.format_for_comment()
    assert "*Waiting for*:" in comment


def test_error_formatting():
    data = ErrorData(details="Package 'invalid-pkg' not found in repository", jira_issue="RHEL-33333")
    result = TriageOutputSchema(resolution=Resolution.ERROR, data=data)

    assert result.format_for_comment() == (
        f"*Resolution*: error\n*Details*: Package 'invalid-pkg' not found in repository{TRIAGE_DISCLAIMER}"
    )


# --- NotAffectedData formatting tests ---


def test_not_affected_formatting():
    data = NotAffectedData(
        justification_category="Vulnerable Code not Present",
        explanation="The vulnerable function foo_parse() was introduced in version 3.2. "
        "This package ships version 3.1, which does not contain the affected code path.",
        jira_issue="RHEL-44444",
    )
    result = TriageOutputSchema(resolution=Resolution.NOT_AFFECTED, data=data)

    assert result.format_for_comment() == (
        "*Recommendation: Not a Bug / Vulnerable Code not Present*\n\n"
        "The vulnerable function foo_parse() was introduced in version 3.2. "
        "This package ships version 3.1, which does not contain the affected code path."
        f"{TRIAGE_DISCLAIMER}"
    )


def test_not_affected_formatting_no_category():
    data = NotAffectedData(
        explanation="Could not conclusively determine the category.",
        jira_issue="RHEL-66666",
    )
    result = TriageOutputSchema(resolution=Resolution.NOT_AFFECTED, data=data)

    comment = result.format_for_comment()
    assert "*Recommendation: Not a Bug / Not Affected*" in comment
    assert "None" not in comment


def test_not_affected_formatting_component_not_present():
    data = NotAffectedData(
        justification_category="Component not Present",
        explanation="The affected subcomponent libfoo-xml is not included in this package build.",
        jira_issue="RHEL-55555",
    )
    result = TriageOutputSchema(resolution=Resolution.NOT_AFFECTED, data=data)

    comment = result.format_for_comment()
    assert "*Recommendation: Not a Bug / Component not Present*" in comment
    assert "libfoo-xml is not included" in comment
    assert TRIAGE_DISCLAIMER in comment


# --- ApplicabilityResult tests ---


def test_applicability_result_not_affected():
    result = ApplicabilityResult(
        is_affected=False,
        justification_category="Vulnerable Code not Present",
        explanation="Function introduced in v3.2, package ships v3.1.",
    )
    assert not result.is_affected
    assert result.justification_category == "Vulnerable Code not Present"


def test_applicability_result_affected():
    result = ApplicabilityResult(
        is_affected=True,
        explanation="The vulnerable code path is present and reachable.",
    )
    assert result.is_affected
    assert result.justification_category is None


def test_applicability_result_roundtrip():
    result = ApplicabilityResult(
        is_affected=False,
        justification_category="Vulnerable Code not in Execute Path",
        explanation="The affected API is imported but never called.",
    )
    json_str = result.model_dump_json()
    restored = ApplicabilityResult.model_validate_json(json_str)
    assert not restored.is_affected
    assert restored.justification_category == "Vulnerable Code not in Execute Path"


# --- RebuildData consolidation tests ---


def test_rebuild_data_all_jira_issues_no_consolidated():
    data = RebuildData(
        package="git-lfs",
        jira_issue="RHEL-100",
        fix_version="rhel-9.8",
    )
    assert data.all_jira_issues == ["RHEL-100"]


def test_rebuild_data_all_jira_issues_with_consolidated():
    data = RebuildData(
        package="git-lfs",
        jira_issue="RHEL-100",
        fix_version="rhel-9.8",
        consolidated_issues=[
            ConsolidatedIssue(
                issue_key="RHEL-101",
                dependency_issue="RHEL-50",
                dependency_component="golang",
            ),
            ConsolidatedIssue(
                issue_key="RHEL-102",
                dependency_issue="RHEL-51",
                dependency_component="golang",
            ),
        ],
    )
    assert data.all_jira_issues == ["RHEL-100", "RHEL-101", "RHEL-102"]


def test_rebuild_data_backward_compat():
    payload = {
        "package": "git-lfs",
        "jira_issue": "RHEL-100",
        "dependency_issue": "RHEL-50",
        "dependency_component": "golang",
        "fix_version": "rhel-9.8",
    }
    data = RebuildData.model_validate(payload)
    assert data.consolidated_issues == []
    assert data.all_jira_issues == ["RHEL-100"]


def test_rebuild_data_serialization_roundtrip():
    data = RebuildData(
        package="git-lfs",
        jira_issue="RHEL-100",
        fix_version="rhel-9.8",
        consolidated_issues=[
            ConsolidatedIssue(
                issue_key="RHEL-101",
                dependency_issue="RHEL-50",
                dependency_component="golang",
            ),
        ],
    )
    json_str = data.model_dump_json()
    restored = RebuildData.model_validate_json(json_str)
    assert restored.all_jira_issues == ["RHEL-100", "RHEL-101"]
    assert restored.consolidated_issues[0].dependency_component == "golang"
