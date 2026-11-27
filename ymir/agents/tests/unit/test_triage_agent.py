import json
import re
from pathlib import Path

import pytest
from flexmock import flexmock
from pydantic import BaseModel
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from ymir.agents import pydantic_agent
from ymir.agents import (
    tasks as agent_tasks,
)
from ymir.agents import (
    triage_agent as t_agent,
)
from ymir.agents.constants import JIRA_COMMENT_TEMPLATE
from ymir.agents.triage_agent import (
    TriageState,
    _build_reproducer_input,
    _defers_fix,
    _map_version_to_module_branch,
    _record_auto_processing_policy,
    _resolution_label,
    determine_target_branch,
    render_prompt,
    validate_triage_output,
)
from ymir.common.constants import YMIR_COMMENT_MARKER, JiraLabels
from ymir.common.models import (
    AssessedApplicabilityResult,
    AutoProcessingMode,
    BackportData,
    ConfidenceAssessment,
    CVEEligibilityResult,
    NotAffectedData,
    PackageAutoProcessingConfig,
    PackageBranchCreationConfig,
    PostponedData,
    RebaseData,
    RebuildData,
    Resolution,
    TriageEligibility,
    TriageInputSchema,
    TriageOutputSchema,
)
from ymir.common.version_utils import extract_downstream_package, is_modular, parse_module_stream


@pytest.mark.asyncio
async def test_pydantic_triage_preserves_tools_and_validates_output():
    class IssueInput(BaseModel):
        issue_key: str

    gateway_tools = [
        flexmock(name=name, description=name, input_schema=IssueInput)
        for name in (
            "get_jira_details",
            "set_jira_fields",
            "get_patch_from_url",
            "get_github_patch",
            "get_github_pull_request",
            "get_github_compare",
            "comment_in_jira",
        )
    ]
    jira_calls = []

    async def run_jira_tool(tool, *, trace_trajectory, **arguments):
        assert trace_trajectory is False
        jira_calls.append((tool.name, arguments["issue_key"]))
        if tool.name == "set_jira_fields":
            return "Gateway response"
        return {"fields": {"summary": _CVE_SUMMARY}}

    flexmock(pydantic_agent).should_receive("run_tool").times(3).replace_with(run_jira_tool)
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        names = {tool.name for tool in info.function_tools}
        assert {
            "get_patch_from_url",
            "get_github_patch",
            "get_github_pull_request",
            "get_github_compare",
        } <= names
        assert "comment_in_jira" not in names
        assert "get_jira_details" in names
        if requests == 1:
            return ModelResponse(parts=[ToolCallPart("get_jira_details", {"issue_key": "RHEL-300091"})])
        if requests == 2:
            return ModelResponse(parts=[ToolCallPart("get_jira_details", {"issue_key": "RHEL-2"})])
        if requests == 3:
            return ModelResponse(parts=[ToolCallPart("set_jira_fields", {"issue_key": "RHEL-2"})])
        if requests == 4:
            assert messages[-1].parts[0].content == "Gateway response"
            return ModelResponse(parts=[TextPart(json.dumps(_backport_answer(jira_issue="RHEL-1")))])
        assert requests == 5
        assert any(isinstance(part, RetryPromptPart) for part in messages[-1].parts)
        return ModelResponse(parts=[TextPart(json.dumps(_backport_answer()))])

    agent = t_agent.create_triage_agent(gateway_tools)
    result = await agent.run(
        "Triage RHEL-300091",
        model=FunctionModel(model),
        deps=t_agent.TriageRunContext("RHEL-300091", "rhel-10.4", _CVE_SUMMARY),
    )

    assert jira_calls == [
        ("get_jira_details", "RHEL-300091"),
        ("get_jira_details", "RHEL-2"),
        ("set_jira_fields", "RHEL-2"),
    ]
    assert result.output.resolution == Resolution.BACKPORT
    assert isinstance(result.output.data, BackportData)
    assert result.output.data.jira_issue == "RHEL-300091"
    assert result.output.data.fix_version == "rhel-10.4"
    assert result.output.data.cve_id == "CVE-2026-115905"


@pytest.mark.parametrize(
    "resolution, expected_label",
    [
        (Resolution.BACKPORT, JiraLabels.TRIAGED_DEFERRED_BACKPORT),
        (Resolution.REBASE, JiraLabels.TRIAGED_DEFERRED_REBASE),
        (Resolution.REBUILD, JiraLabels.TRIAGED_DEFERRED_REBUILD),
        # Resolutions without a deferred variant keep their normal terminal label.
        (Resolution.NOT_AFFECTED, JiraLabels.TRIAGED_NOT_AFFECTED),
    ],
)
def test_resolution_label_uses_deferred_variant_in_triage_only_mode(resolution, expected_label):
    assert _resolution_label(resolution, triage_only=True) is expected_label


@pytest.mark.parametrize("resolution", [Resolution.BACKPORT, Resolution.REBASE, Resolution.REBUILD])
def test_resolution_label_unchanged_outside_triage_only_mode(resolution):
    assert _resolution_label(resolution, triage_only=False) is t_agent._RESOLUTION_TO_LABEL[resolution]


@pytest.mark.parametrize("resolution", [Resolution.BACKPORT, Resolution.REBASE, Resolution.REBUILD])
def test_defers_fix_for_fix_resolutions(resolution):
    assert _defers_fix(resolution, triage_only=True) is True


@pytest.mark.parametrize(
    "resolution", [Resolution.NOT_AFFECTED, Resolution.CLARIFICATION_NEEDED, Resolution.OPEN_ENDED_ANALYSIS]
)
def test_triage_only_does_not_advertise_ymir_todo_without_a_fix(resolution):
    """No fix was deferred, so telling the maintainer to add ymir_todo is wrong."""
    assert _defers_fix(resolution, triage_only=True) is False


def test_triage_only_never_holds_for_manual_branch_creation():
    """Triage-only runs stop after triage, so a missing branch must not hold them."""
    state = TriageState(
        jira_issue="RHEL-102",
        target_branch="rhel-9.6.0.z",
        target_branch_exists=False,
        automatic_branch_creation_disabled=True,
        triage_result=TriageOutputSchema(
            resolution=Resolution.REBASE,
            data=RebaseData(package="httpd", version="2.4.62", jira_issue="RHEL-102"),
        ),
    )

    assert state.hold_for_manual_branch_creation is True
    state.triage_only = True
    assert state.hold_for_manual_branch_creation is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_mode, user_triggered, triage_only_requested, expected",
    [
        # Automatic runs follow the package config.
        (AutoProcessingMode.TRIAGE_ONLY, False, False, True),
        (AutoProcessingMode.FULL, False, False, False),
        # ymir_todo forces the full pipeline, even for triage-only packages.
        (AutoProcessingMode.TRIAGE_ONLY, True, False, False),
        # ymir_todo_triage forces triage-only, even for full-mode packages.
        (AutoProcessingMode.FULL, True, True, True),
        (AutoProcessingMode.TRIAGE_ONLY, True, True, True),
    ],
)
async def test_auto_processing_precedence(
    monkeypatch, config_mode, user_triggered, triage_only_requested, expected
):
    """An explicit trigger overrides the package config; automatic runs follow it."""

    async def _fake_fetch(*_args, **_kwargs):
        return PackageAutoProcessingConfig(mode=config_mode)

    async def _fake_metadata(*_args, **_kwargs):
        return [], "New"

    monkeypatch.setattr(agent_tasks, "fetch_auto_processing_config", _fake_fetch)
    monkeypatch.setattr(agent_tasks, "get_jira_issue_metadata", _fake_metadata)

    state = TriageState(jira_issue="RHEL-99999")
    await _record_auto_processing_policy(state, "bash", [], user_triggered, triage_only_requested)

    assert state.triage_only is expected


@pytest.mark.asyncio
async def test_auto_processing_marker_forces_triage_only_on_automatic_run(monkeypatch):
    """The persisted per-issue marker keeps an automatic re-entry triage-only
    even when the package is configured for full processing."""

    async def _fake_fetch(*_args, **_kwargs):
        return PackageAutoProcessingConfig(mode=AutoProcessingMode.FULL)

    async def _fake_metadata(*_args, **_kwargs):
        return [JiraLabels.TRIAGE_ONLY.value], "New"

    monkeypatch.setattr(agent_tasks, "fetch_auto_processing_config", _fake_fetch)
    monkeypatch.setattr(agent_tasks, "get_jira_issue_metadata", _fake_metadata)

    state = TriageState(jira_issue="RHEL-99999")
    await _record_auto_processing_policy(state, "bash", [], user_triggered=False)

    assert state.triage_only is True


@pytest.mark.asyncio
async def test_auto_processing_marker_ignored_for_user_triggered_run(monkeypatch):
    """ymir_todo is the explicit full-pipeline request, so a leftover marker
    must not keep that run triage-only."""

    async def _fake_fetch(*_args, **_kwargs):
        return PackageAutoProcessingConfig(mode=AutoProcessingMode.TRIAGE_ONLY)

    async def _fake_metadata(*_args, **_kwargs):
        return [JiraLabels.TRIAGE_ONLY.value], "New"

    monkeypatch.setattr(agent_tasks, "fetch_auto_processing_config", _fake_fetch)
    monkeypatch.setattr(agent_tasks, "get_jira_issue_metadata", _fake_metadata)

    state = TriageState(jira_issue="RHEL-99999")
    await _record_auto_processing_policy(state, "bash", [], user_triggered=True)

    assert state.triage_only is False


@pytest.mark.asyncio
async def test_auto_processing_retries_when_marker_read_fails():
    """An unreadable marker must not fall back to the package config.

    The package may be configured for full processing, so guessing would start a
    fix for an issue whose triage-only marker could not be ruled out. The labels
    must be read strictly and the failure must reach the task retry path.
    """

    flexmock(agent_tasks).should_receive("fetch_auto_processing_config").never()
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").with_args(
        "RHEL-99999", raise_on_error=True
    ).and_raise(RuntimeError("Jira unavailable")).once()

    state = TriageState(jira_issue="RHEL-99999")
    with pytest.raises(RuntimeError, match="Jira unavailable"):
        await _record_auto_processing_policy(state, "bash", [], user_triggered=False)


@pytest.mark.asyncio
async def test_auto_processing_user_triggered_run_does_not_need_rules_repo(monkeypatch):
    """An explicit ymir_todo must still run while the rules repo is unavailable."""

    async def _failing_fetch(*_args, **_kwargs):
        raise RuntimeError("Failed to fetch maintainer rules (HTTP 500)")

    monkeypatch.setattr(agent_tasks, "fetch_auto_processing_config", _failing_fetch)

    state = TriageState(jira_issue="RHEL-99999")
    await _record_auto_processing_policy(state, "bash", [], user_triggered=True)

    assert state.triage_only is False


# --- Modular detection tests ---


@pytest.mark.parametrize(
    "summary, downstream_component, expected",
    [
        ("postgresql:12/postgresql:PostgreSQL: Arbitrary code execution", "postgresql", True),
        ("postgresql:12.0/postgresql:PostgreSQL: some vulnerability", "postgresql", True),
        ("nodejs:18/nodejs:Node.js: buffer overflow", "nodejs", True),
        ("perl-DBD-MySQL:8.0/perl-DBD-MySQL:Fix for crash", "perl-DBD-MySQL", True),
        ("ruby:3.1-beta/ruby:Ruby: CVE fix", "ruby", True),
        ("python3.11:3.11/python3.11:Python: CVE fix", "python3.11", True),
        ("gcc-c++:10/gcc-c++:GCC: CVE fix", "gcc-c++", True),
        (
            "CVE-2026-32748 squid:4/squid: Squid: Denial of Service via crafted ICP traffic [rhel-8.10.z]",
            "squid",
            True,
        ),
        # Package after slash must match Downstream Component Name
        ("postgresql:12/postgresql:PostgreSQL: vuln", "nginx", False),
        ("postgresql:PostgreSQL: Arbitrary code execution", "postgresql", False),
        ("CVE-2025-9900 libtiff: Libtiff Write-What-Where [rhel-9.2.0.z]", "libtiff", False),
        ("some plain summary without colons", "nginx", False),
        ("postgresql:12/postgresql:vuln", None, False),
        ("", "postgresql", False),
        (None, "postgresql", False),
        ("postgresql:12/postgresql:vuln", "", False),
    ],
)
def test_is_modular(summary, downstream_component, expected):
    assert is_modular(summary, downstream_component) is expected


# --- Module summary parsing tests ---


@pytest.mark.parametrize(
    "summary, downstream_component, expected_module, expected_stream",
    [
        ("postgresql:12/postgresql:PostgreSQL: vuln", "postgresql", "postgresql", "12"),
        ("nodejs:18/nodejs:Node.js: issue", "nodejs", "nodejs", "18"),
        ("perl-DBD-MySQL:8.0/perl-DBD-MySQL:Fix", "perl-DBD-MySQL", "perl-DBD-MySQL", "8.0"),
        ("ruby:3.1-beta/ruby:Ruby: CVE", "ruby", "ruby", "3.1-beta"),
        ("python3.11:3.11/python3.11:Python: CVE", "python3.11", "python3.11", "3.11"),
        ("gcc-c++:10/gcc-c++:GCC: CVE", "gcc-c++", "gcc-c++", "10"),
        (
            "CVE-2026-32748 squid:4/squid: Squid: Denial of Service [rhel-8.10.z]",
            "squid",
            "squid",
            "4",
        ),
        # Component package differs from module name
        (
            "perl:5.32/perl-IO-Socket-SSL:Fix for crash",
            "perl-IO-Socket-SSL",
            "perl",
            "5.32",
        ),
    ],
)
def test_parse_module_summary(summary, downstream_component, expected_module, expected_stream):
    result = parse_module_stream(summary, downstream_component)
    assert result is not None
    module, stream = result
    assert module == expected_module
    assert stream == expected_stream


def test_parse_module_summary_non_modular():
    assert parse_module_stream("postgresql:PostgreSQL: vuln", "postgresql") is None


def test_parse_module_summary_package_mismatch():
    assert parse_module_stream("postgresql:12/postgresql:vuln", "nginx") is None


# --- Modular branch mapping tests ---


@pytest.mark.parametrize(
    "version, summary, downstream_component, expected_branch",
    [
        (
            "rhel-9.8",
            "postgresql:12/postgresql:PostgreSQL: vuln",
            "postgresql",
            "stream-postgresql-12-rhel-9.8.0",
        ),
        (
            "rhel-9.9",
            "postgresql:12/postgresql:PostgreSQL: vuln",
            "postgresql",
            "stream-postgresql-12-rhel-9.9.0",
        ),
        (
            "rhel-10.2",
            "nodejs:18/nodejs:Node.js: issue",
            "nodejs",
            "stream-nodejs-18-rhel-10.2.0",
        ),
        (
            "rhel-9.8.z",
            "postgresql:12/postgresql:PostgreSQL: vuln",
            "postgresql",
            "stream-postgresql-12-rhel-9.8.0",
        ),
        (
            "rhel-8.10.z",
            "CVE-2026-32748 squid:4/squid: Squid: Denial of Service [rhel-8.10.z]",
            "squid",
            "stream-squid-4-rhel-8.10.0",
        ),
    ],
)
def test_map_version_to_module_branch(version, summary, downstream_component, expected_branch):
    branch = _map_version_to_module_branch(version, summary, downstream_component)
    assert branch == expected_branch


def test_map_version_to_module_branch_invalid_version():
    branch = _map_version_to_module_branch("not-a-version", "postgresql:12/postgresql:vuln", "postgresql")
    assert branch is None


def test_map_version_to_module_branch_extracts_package_from_raw_field():
    """customfield_10669 stores module:stream/package; mapping needs the package."""
    raw = "postgresql:16/postgis"
    summary = "postgresql:16/postgis: PostGIS: vuln"
    assert _map_version_to_module_branch("rhel-9.8", summary, raw) is None
    package = extract_downstream_package(raw)
    assert _map_version_to_module_branch("rhel-9.8", summary, package) == "stream-postgresql-16-rhel-9.8.0"


# --- Modular target branch + namespace selection ---


def _modular_backport_data(
    fix_version: str = "rhel-8.10.z",
) -> BackportData:
    return BackportData(
        package="squid",
        patch_urls=["https://example.com/fix.patch"],
        justification="test",
        jira_issue="RHEL-160675",
        cve_id="CVE-2026-32748",
        fix_version=fix_version,
    )


_MODULAR_SUMMARY = "CVE-2026-32748 squid:4/squid: Squid: Denial of Service [rhel-8.10.z]"


def _cve_eligibility(*, needs_internal_fix: bool) -> CVEEligibilityResult:
    return CVEEligibilityResult(
        is_cve=True,
        eligibility=TriageEligibility.IMMEDIATELY,
        reason="test",
        needs_internal_fix=needs_internal_fix,
    )


async def _older_zstream_true(*_args, **_kwargs):
    return True


async def _older_zstream_false(*_args, **_kwargs):
    return False


@pytest.mark.asyncio
async def test_determine_target_branch_modular_internal_fix_no_ystream_uses_cs():
    """RHEL 8 has no Y-stream, so even CVEs needing internal fix go to centos-stream."""

    async def _mock_load_rhel_config(*_args, **_kwargs):
        return {
            "current_y_streams": {"9": "rhel-9.9", "10": "rhel-10.3"},
            "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.8.z", "10": "rhel-10.2.z"},
        }

    flexmock(t_agent).should_receive("is_older_zstream").replace_with(_older_zstream_false)
    flexmock(t_agent).should_receive("load_rhel_config").replace_with(_mock_load_rhel_config)

    branch, namespace = await determine_target_branch(
        _cve_eligibility(needs_internal_fix=True),
        _modular_backport_data(),
        jira_summary=_MODULAR_SUMMARY,
        downstream_component="squid",
    )
    assert branch == "stream-squid-4-rhel-8.10.0"
    assert namespace == "centos-stream"


@pytest.mark.asyncio
async def test_determine_target_branch_modular_internal_fix_with_ystream_uses_rhel():
    """RHEL 9 has a Y-stream, so CVEs needing internal fix go to rhel."""

    async def _mock_load_rhel_config(*_args, **_kwargs):
        return {"current_y_streams": {"9": "rhel-9.9", "10": "rhel-10.3"}}

    summary = "CVE-2026-32748 squid:4/squid: Squid: Denial of Service [rhel-9.8.z]"

    flexmock(t_agent).should_receive("is_older_zstream").replace_with(_older_zstream_false)
    flexmock(t_agent).should_receive("load_rhel_config").replace_with(_mock_load_rhel_config)

    branch, namespace = await determine_target_branch(
        _cve_eligibility(needs_internal_fix=True),
        _modular_backport_data(fix_version="rhel-9.8.z"),
        jira_summary=summary,
        downstream_component="squid",
    )
    assert branch == "stream-squid-4-rhel-9.8.0"
    assert namespace == "rhel"


@pytest.mark.asyncio
async def test_determine_target_branch_modular_cs_eligible_uses_centos_stream():
    flexmock(t_agent).should_receive("is_older_zstream").replace_with(_older_zstream_false)

    branch, namespace = await determine_target_branch(
        _cve_eligibility(needs_internal_fix=False),
        _modular_backport_data(),
        jira_summary=_MODULAR_SUMMARY,
        downstream_component="squid",
    )
    assert branch == "stream-squid-4-rhel-8.10.0"
    assert namespace == "centos-stream"


@pytest.mark.asyncio
async def test_determine_target_branch_modular_older_zstream_uses_rhel():
    flexmock(t_agent).should_receive("is_older_zstream").replace_with(_older_zstream_true)

    branch, namespace = await determine_target_branch(
        _cve_eligibility(needs_internal_fix=False),
        _modular_backport_data(fix_version="rhel-8.6.z"),
        jira_summary=_MODULAR_SUMMARY,
        downstream_component="squid",
    )
    assert branch == "stream-squid-4-rhel-8.6.0"
    assert namespace == "rhel"


@pytest.mark.asyncio
async def test_render_prompt_modular_rhel8_no_internal_fix():
    """RHEL 8 has no Y-stream, so render_prompt must NOT set needs_internal_fix
    for modular issues even when CVE eligibility says needs_internal_fix=True.
    Otherwise the prompt tells the LLM to clone from rhel namespace instead of
    centos-stream."""

    async def _mock_load_rhel_config(*_args, **_kwargs):
        return {
            "current_y_streams": {"9": "rhel-9.9", "10": "rhel-10.3"},
            "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.8.z", "10": "rhel-10.2.z"},
        }

    input_data = TriageInputSchema(issue="RHEL-999")
    summary = "CVE-2026-32748 squid:4/squid: Denial of Service [rhel-8.10.z]"

    flexmock(t_agent).should_receive("is_older_zstream").replace_with(_older_zstream_false)
    flexmock(t_agent).should_receive("load_rhel_config").replace_with(_mock_load_rhel_config)

    prompt = await render_prompt(
        input_data,
        fix_version="rhel-8.10.z",
        cve_eligibility_result=_cve_eligibility(needs_internal_fix=True),
        jira_summary=summary,
        downstream_component="squid",
    )
    assert "stream-squid-4-rhel-8.10.0" not in prompt
    assert "redhat/rhel/rpms" not in prompt


@pytest.mark.asyncio
async def test_render_prompt_modular_rhel9_has_internal_fix():
    """RHEL 9 has a Y-stream, so render_prompt SHOULD set needs_internal_fix
    for modular issues when CVE eligibility says needs_internal_fix=True."""

    async def _mock_load_rhel_config(*_args, **_kwargs):
        return {"current_y_streams": {"9": "rhel-9.9", "10": "rhel-10.3"}}

    input_data = TriageInputSchema(issue="RHEL-999")
    summary = "CVE-2026-32748 squid:4/squid: Denial of Service [rhel-9.8.z]"

    flexmock(t_agent).should_receive("is_older_zstream").replace_with(_older_zstream_false)
    flexmock(t_agent).should_receive("load_rhel_config").replace_with(_mock_load_rhel_config)

    prompt = await render_prompt(
        input_data,
        fix_version="rhel-9.8.z",
        cve_eligibility_result=_cve_eligibility(needs_internal_fix=True),
        jira_summary=summary,
        downstream_component="squid",
    )
    assert "stream-squid-4-rhel-9.8.0" in prompt


@pytest.mark.asyncio
async def test_determine_target_branch_non_modular_has_no_explicit_namespace():
    data = BackportData(
        package="nginx",
        patch_urls=["https://example.com/fix.patch"],
        justification="test",
        jira_issue="RHEL-1",
        cve_id="CVE-2026-1",
        fix_version="rhel-10.2.z",
    )

    async def _mock_version_to_branch(*_args, **_kwargs):
        return "rhel-10.2"

    flexmock(t_agent).should_receive("_map_version_to_branch").replace_with(_mock_version_to_branch)

    branch, namespace = await determine_target_branch(
        _cve_eligibility(needs_internal_fix=True),
        data,
        jira_summary="CVE-2026-1 nginx: something [rhel-10.2.z]",
        downstream_component="nginx",
    )
    assert branch == "rhel-10.2"
    assert namespace is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("branch", "available_branches", "expected"),
    [
        ("rhel-9.9.0", ["rhel-9.9.0"], True),
        ("rhel-10.3", [], False),
        ("c10s", ["rhel-10.3"], None),
    ],
)
async def test_record_target_branch_existence_only_checks_internal_zstreams(
    branch, available_branches, expected
):
    state = TriageState(jira_issue="RHEL-100", target_branch=branch)

    async def _mock_run_tool(*_args, **_kwargs):
        return available_branches

    flexmock(t_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    await t_agent._record_target_branch_existence(state, "bash", [])

    assert state.target_branch_exists is expected


# --- Per-issue lock tests ---


def test_build_reproducer_input_from_backport():
    state = TriageState(
        jira_issue="RHEL-100",
        target_branch="c10s",
        triage_result=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="bind",
                patch_urls=["https://example.com/a.patch"],
                justification="fixes overflow",
                triage_summary="Looked at upstream commit.",
                jira_issue="RHEL-100",
                cve_id="CVE-2025-1",
                fix_version="rhel-10.1",
            ),
        ),
    )
    payload = _build_reproducer_input(state)
    assert payload is not None
    assert payload.package == "bind"
    assert payload.cve_id == "CVE-2025-1"
    assert payload.patch_urls == ["https://example.com/a.patch"]
    assert payload.triage_summary == "Looked at upstream commit."
    assert payload.target_branch == "c10s"


def test_build_reproducer_input_from_rebase_and_rebuild():
    rebase_state = TriageState(
        jira_issue="RHEL-101",
        target_branch="c9s",
        triage_result=TriageOutputSchema(
            resolution=Resolution.REBASE,
            data=RebaseData(
                package="httpd",
                version="2.4.62",
                jira_issue="RHEL-101",
                cve_id="CVE-2025-2",
                fix_version="rhel-9.6",
            ),
        ),
    )
    rebuild_state = TriageState(
        jira_issue="RHEL-102",
        target_branch="c10s",
        triage_result=TriageOutputSchema(
            resolution=Resolution.REBUILD,
            data=RebuildData(
                package="podman",
                jira_issue="RHEL-102",
                cve_id="CVE-2025-3",
                fix_version="rhel-10.1",
            ),
        ),
    )
    assert _build_reproducer_input(rebase_state).package == "httpd"
    assert _build_reproducer_input(rebuild_state).package == "podman"


def test_build_reproducer_input_from_not_affected_includes_explanation():
    state = TriageState(
        jira_issue="RHEL-103",
        target_branch="c10s",
        triage_result=TriageOutputSchema(
            resolution=Resolution.NOT_AFFECTED,
            data=NotAffectedData(
                justification_category="Vulnerable Code not Present",
                explanation="Function parse_header is not in this build.",
                jira_issue="RHEL-103",
                package="libfoo",
                cve_id="CVE-2025-4",
                fix_version="rhel-10.1",
                triage_summary="Checked sources.",
            ),
        ),
    )
    payload = _build_reproducer_input(state)
    assert payload is not None
    assert payload.package == "libfoo"
    assert "not-affected" in payload.triage_summary
    assert "Vulnerable Code not Present" in payload.triage_summary
    assert "Function parse_header" in payload.triage_summary


def test_build_reproducer_input_skips_without_package():
    state = TriageState(
        jira_issue="RHEL-104",
        triage_result=TriageOutputSchema(
            resolution=Resolution.NOT_AFFECTED,
            data=NotAffectedData(
                explanation="no package",
                jira_issue="RHEL-104",
            ),
        ),
    )
    assert _build_reproducer_input(state) is None


def test_build_reproducer_input_skips_postponed():
    """Helper itself does not filter resolution; enqueue gate does. Still builds if package set."""
    state = TriageState(
        jira_issue="RHEL-105",
        triage_result=TriageOutputSchema(
            resolution=Resolution.POSTPONED_DEPENDENCY,
            data=PostponedData(
                summary="waiting",
                pending_issues=["RHEL-1"],
                jira_issue="RHEL-105",
                package="golang",
            ),
        ),
    )
    # Builder returns a payload when package exists; eligibility is checked by enqueue.
    assert _build_reproducer_input(state).package == "golang"


# --- Eligibility → resolution mapping regression tests ---


def test_pending_dependencies_maps_to_postponed_y_stream():
    pending_issues = ["RHEL-99998"]
    state = TriageState(
        jira_issue="RHEL-99999",
        cve_eligibility_result=CVEEligibilityResult(
            is_cve=True,
            eligibility=TriageEligibility.PENDING_DEPENDENCIES,
            reason="Waiting for Z-stream clones to ship",
            pending_zstream_issues=pending_issues,
        ),
    )

    t_agent.apply_cve_eligibility(state, force_cve_triage=False)

    assert state.triage_result.resolution == Resolution.POSTPONED_Y_STREAM
    assert state.triage_result.data.pending_issues == pending_issues


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_resolution", "is_affected", "in_buildroot", "final_resolution"),
    [
        (Resolution.BACKPORT, True, True, Resolution.BACKPORT),
        (Resolution.REBASE, True, True, Resolution.REBASE),
        (Resolution.REBUILD, True, True, Resolution.REBUILD),
        (Resolution.REBUILD, False, True, Resolution.NOT_AFFECTED),
        (Resolution.REBUILD, True, False, Resolution.POSTPONED_DEPENDENCY),
    ],
)
async def test_manual_branch_hold_uses_final_resolution(
    initial_resolution, is_affected, in_buildroot, final_resolution
):
    common = {"package": "bash", "jira_issue": "RHEL-99999", "fix_version": "rhel-10.3.z"}
    if initial_resolution == Resolution.BACKPORT:
        data = BackportData(**common, patch_urls=[], justification="Apply upstream fix")
    elif initial_resolution == Resolution.REBASE:
        data = RebaseData(**common, version="5.3")
    else:
        data = RebuildData(**common, dependency_issue="RHEL-1", dependency_component="openssl")
    state = TriageState(
        jira_issue=common["jira_issue"],
        triage_result=TriageOutputSchema(resolution=initial_resolution, data=data),
    )
    confidence = ConfidenceAssessment(level="high", reason="Inspected target source and build paths")

    async def determine_branch(**_kwargs):
        return "rhel-10.3", "rhel"

    async def list_branches(name, *, available_tools, package):
        assert name == "get_internal_rhel_branches"
        assert package == "bash"
        return []

    async def branch_policy(package, available_tools):
        assert package == "bash"
        return PackageBranchCreationConfig(automatic=False)

    flexmock(t_agent).should_receive("determine_target_branch").replace_with(determine_branch)
    flexmock(t_agent).should_receive("run_tool").replace_with(list_branches)
    flexmock(agent_tasks).should_receive("fetch_branch_creation_config").replace_with(branch_policy)
    await t_agent.resolve_triage_target(state, [], user_triggered=True, triage_only_requested=False)
    assert state.hold_for_manual_branch_creation

    next_step = t_agent.apply_cve_applicability(
        state,
        AssessedApplicabilityResult(
            is_affected=is_affected, explanation="Source analysis result", confidence=confidence
        ),
    )
    if next_step == "verify_rebuild_buildroot":
        t_agent.apply_rebuild_buildroot(state, (in_buildroot, "openssl-3.0-1.el10"))

    assert state.triage_result.resolution == final_resolution
    assert state.triage_result.confidence == confidence
    assert state.hold_for_manual_branch_creation is (final_resolution == initial_resolution)


def test_apply_triage_output_rejects_pr_pending_without_blocker_reference():
    state = TriageState(jira_issue="RHEL-99999")
    output = TriageOutputSchema(
        resolution=Resolution.POSTPONED_PR_PENDING,
        data=PostponedData(
            summary="Waiting for upstream PR", pending_issues=["RHEL-1"], jira_issue=state.jira_issue
        ),
    )

    with pytest.raises(ValueError, match=r"postponed_pr_pending.*blocker MR/PR URL"):
        t_agent.apply_triage_output(state, output)


def test_rebase_triage_ignores_model_proposed_siblings():
    normalized = validate_triage_output(
        json.dumps(
            {
                "resolution": "rebase",
                "data": {
                    "package": "bash",
                    "version": "5.3",
                    "consolidated_issues": [{"issue_key": "RHEL-2"}],
                },
            }
        ),
        jira_issue="RHEL-1",
    )

    data = TriageOutputSchema.model_validate_json(normalized).data
    assert isinstance(data, RebaseData)
    assert data.all_jira_issues == ["RHEL-1"]


def test_ymir_comment_marker_triage():
    """The shared marker template renders the exact string the sweep matches."""
    assert YMIR_COMMENT_MARKER.substitute(AGENT_TYPE="Triage") == "Output from Ymir Triage Agent"


def test_jira_comment_template_output_unchanged():
    """Rebuilding JIRA_COMMENT_TEMPLATE from the shared marker keeps output identical."""
    rendered = JIRA_COMMENT_TEMPLATE.substitute(AGENT_TYPE="Triage", JIRA_COMMENT="hello")
    assert rendered.startswith("Output from Ymir Triage Agent: \n\nhello\n\n")


_CVE_SUMMARY = "CVE-2026-115905 keylime-agent-rust: zip: parser differential [rhel-10.4]"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefetch", ["success", "malformed", "error"])
async def test_prepare_triage_analysis_includes_jira_context_when_available(prefetch):
    state = TriageState(jira_issue="RHEL-300091")
    details = {
        "key": state.jira_issue,
        "fields": {
            "summary": _CVE_SUMMARY,
            "fixVersions": [{"name": "rhel-10.4"}],
            "comment": {"comments": [{"body": "Maintainer supplied patch details"}]},
        },
        "remote_links": [{"object": {"url": "https://example.com/change"}}],
    }

    async def read_issue(name, *, available_tools, issue_key):
        assert name == "get_jira_details"
        assert issue_key == state.jira_issue
        if prefetch == "error":
            raise TimeoutError("Jira request timed out")
        return details if prefetch == "success" else {"error": "Jira returned no issue"}

    async def is_older_zstream(_version):
        return False

    flexmock(t_agent).should_receive("run_tool").replace_with(read_issue).once()
    flexmock(t_agent).should_receive("is_older_zstream").replace_with(is_older_zstream)

    prompt, deps = await t_agent.prepare_triage_analysis(state, [])

    assert deps.jira_issue == state.jira_issue
    if prefetch == "success":
        assert json.loads(prompt.split("Jira issue data (JSON):\n", 1)[1]) == details
        assert deps.fix_version == "rhel-10.4"
        assert deps.jira_summary == state.jira_summary == _CVE_SUMMARY
    else:
        assert "Jira issue data (JSON):" not in prompt
        assert "If it is missing, fetch the issue using get_jira_details." in prompt
        assert deps.fix_version is None
        assert deps.jira_summary is None


def _backport_answer(**data_overrides) -> dict:
    """Backport answer shaped like the one in trace aceb8197 (RHEL-300091)."""
    data = {
        "package": "keylime-agent-rust",
        "patch_urls": [
            "https://github.com/zip-rs/zip2/commit/8068ab480ccf8daa7411424e365fcdbe6d09443e.patch"
        ],
        "justification": "Adds LFH/CDFH header consistency validation.",
        "triage_summary": "Vendored zip 0.6.6 needs a patch.",
    }
    data.update(data_overrides)
    return {"resolution": "backport", "data": data}


def test_validate_triage_output_fills_fields_known_to_workflow():
    normalized = validate_triage_output(
        json.dumps(_backport_answer()),
        jira_issue="RHEL-300091",
        fix_version="rhel-10.4",
        jira_summary=_CVE_SUMMARY,
    )

    output = TriageOutputSchema.model_validate_json(normalized)
    assert output.resolution == Resolution.BACKPORT
    assert isinstance(output.data, BackportData)
    assert output.data.jira_issue == "RHEL-300091"
    assert output.data.fix_version == "rhel-10.4"
    assert output.data.cve_id == "CVE-2026-115905"


def test_validate_triage_output_keeps_values_from_model():
    answer = _backport_answer(jira_issue="RHEL-1", fix_version="rhel-10.3", cve_id="CVE-2026-1 CVE-2026-2")

    normalized = validate_triage_output(
        json.dumps(answer), jira_issue="RHEL-300091", fix_version="rhel-10.4", jira_summary=_CVE_SUMMARY
    )

    data = json.loads(normalized)["data"]
    assert data["jira_issue"] == "RHEL-1"
    assert data["fix_version"] == "rhel-10.3"
    assert data["cve_id"] == "CVE-2026-1 CVE-2026-2"


def test_validate_triage_output_only_fills_fields_the_resolution_has():
    answer = {"resolution": "error", "data": {"details": "Package not found."}}

    normalized = validate_triage_output(
        json.dumps(answer), jira_issue="RHEL-1", fix_version="rhel-10.4", jira_summary=_CVE_SUMMARY
    )

    assert json.loads(normalized)["data"] == {"details": "Package not found.", "jira_issue": "RHEL-1"}


def test_validate_triage_output_no_cve_in_summary():
    normalized = validate_triage_output(
        json.dumps(_backport_answer()), jira_issue="RHEL-1", jira_summary="Fix crash in foo"
    )

    data = json.loads(normalized)["data"]
    assert "cve_id" not in data
    assert "fix_version" not in data


def test_validate_triage_output_reports_missing_field_for_chosen_resolution():
    answer = _backport_answer()
    del answer["data"]["patch_urls"]

    with pytest.raises(ValueError) as excinfo:
        validate_triage_output(json.dumps(answer), jira_issue="RHEL-1")

    message = str(excinfo.value)
    assert "'backport'" in message
    assert "data.patch_urls: Field required" in message
    # Only the chosen resolution's schema is reported, not every union member.
    assert "RebaseData" not in message


@pytest.mark.parametrize(
    "text, expected",
    [
        ("not json", "not valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ('{"resolution": "fix-it", "data": {}}', "Unknown resolution 'fix-it'"),
        ('{"resolution": "backport", "data": "{\\"package\\": \\"x\\"}"}', "`data` must be a JSON object"),
    ],
)
def test_validate_triage_output_rejects_malformed_answers(text, expected):
    with pytest.raises(ValueError, match=expected):
        validate_triage_output(text, jira_issue="RHEL-1")


def test_validate_triage_output_keeps_resolution_model_despite_extra_keys():
    """A stray `version` must not turn a rebuild answer into RebaseData."""
    answer = {
        "resolution": "rebuild",
        "data": {"package": "foo", "version": "1.0", "fix_version": "rhel-9.8"},
    }

    normalized = validate_triage_output(json.dumps(answer), jira_issue="RHEL-1")

    output = TriageOutputSchema.model_validate_json(normalized)
    assert isinstance(output.data, RebuildData)
    assert output.data.jira_issue == "RHEL-1"
    assert "version" not in json.loads(normalized)["data"]


def _output_format_examples() -> list[dict]:
    template = Path(t_agent.__file__).parent / "prompts" / "triage" / "output_format.j2"
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)```", template.read_text(), re.DOTALL)]


@pytest.mark.parametrize("example", _output_format_examples(), ids=lambda example: example["resolution"])
def test_validate_triage_output_accepts_prompt_examples(example):
    expected_model = t_agent._RESOLUTION_DATA_MODELS[Resolution(example["resolution"])]

    normalized = validate_triage_output(json.dumps(example), jira_issue=example["data"]["jira_issue"])

    assert type(TriageOutputSchema.model_validate_json(normalized).data) is expected_model


def test_validate_triage_output_rejects_postponed_y_stream():
    answer = {
        "resolution": "postponed_y_stream",
        "data": {"summary": "Waiting for Z-stream", "pending_issues": ["RHEL-2"]},
    }

    with pytest.raises(ValueError, match=r"set by the eligibility check.*use open-ended-analysis"):
        validate_triage_output(json.dumps(answer), jira_issue="RHEL-1")


def test_validate_triage_output_pr_pending_requires_blocker_references():
    data = {"summary": "Fix pending in upstream PR", "pending_issues": ["RHEL-1"]}
    answer = {"resolution": "postponed_pr_pending", "data": data}

    with pytest.raises(ValueError, match=r"data\.blocker_references"):
        validate_triage_output(json.dumps(answer), jira_issue="RHEL-1")

    data["blocker_references"] = ["https://github.com/org/repo/pull/42"]
    normalized = validate_triage_output(json.dumps(answer), jira_issue="RHEL-1")
    assert (
        TriageOutputSchema.model_validate_json(normalized).data.blocker_references
        == data["blocker_references"]
    )
