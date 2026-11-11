import pytest
from flexmock import flexmock

from ymir.agents import rebuild_consolidation
from ymir.agents.rebuild_agent import _clear_rebuild_resolved_errors
from ymir.agents.rebuild_consolidation import (
    _build_sibling_analysis_prompt,
    build_rebuild_siblings_jql,
    find_rebuild_siblings,
)
from ymir.common.models import ConsolidatedIssue, RebuildData


def test_build_rebuild_siblings_jql():
    jql = build_rebuild_siblings_jql("RHEL-100", "git-lfs", "rhel-9.8")
    assert 'component = "git-lfs"' in jql
    assert 'fixVersion in ("rhel-9.8", "rhel-9.8.z")' in jql
    assert 'key != "RHEL-100"' in jql
    assert 'labels = "SecurityTracking"' in jql
    assert "labels not in" in jql
    assert '"ymir_triaged_rebuild"' not in jql
    assert '"ymir_rebuilt"' not in jql
    assert '"ymir_triaged_not_affected"' in jql
    assert '"ymir_triaged_deferred_rebuild"' in jql
    assert '"ymir_triaged_deferred_rebase"' in jql
    assert '"ymir_triaged_deferred_backport"' in jql
    assert '"ymir_triage_only"' in jql
    assert '"ymir_triaged_backport"' in jql
    assert '"ymir_triaged_rebase"' in jql
    assert 'status in ("New", "Planning")' in jql


def test_build_rebuild_siblings_jql_filters_modular_stream():
    jql = build_rebuild_siblings_jql(
        "RHEL-100", "postgis", "rhel-9.8", downstream_component="postgresql:16/postgis"
    )
    assert 'cf[10669] = "postgresql:16/postgis"' in jql
    assert 'component = "postgis"' in jql


def test_build_rebuild_siblings_jql_no_filter_for_nonmodular():
    jql = build_rebuild_siblings_jql("RHEL-100", "curl", "rhel-9.8", downstream_component="curl")
    assert "cf[10669]" not in jql


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_successful_consolidated_rebuild_clears_primary_and_distinct_siblings(dry_run):
    calls = []

    async def mock_clear(redis_conn, issue, queue, *, target_branch, dry_run):
        calls.append((redis_conn, issue, queue, target_branch, dry_run))

    from ymir.agents import rebuild_agent

    flexmock(rebuild_agent).should_receive("clear_resolved_errors").replace_with(mock_clear).once()
    redis = object()
    data = RebuildData(
        package="git-lfs",
        jira_issue="RHEL-100",
        consolidated_issues=[
            ConsolidatedIssue(issue_key="RHEL-200"),
            ConsolidatedIssue(issue_key="RHEL-100"),
            ConsolidatedIssue(issue_key="RHEL-200"),
            ConsolidatedIssue(issue_key="RHEL-300"),
        ],
    )

    await _clear_rebuild_resolved_errors(
        redis, data, "rebuild_queue_c10s", target_branch="rhel-10.3", dry_run=dry_run
    )

    assert calls == [(redis, data.all_jira_issues, "rebuild_queue_c10s", "rhel-10.3", dry_run)]


def test_build_rebuild_siblings_jql_escapes_component_quotes():
    jql = build_rebuild_siblings_jql("RHEL-100", 'comp"name', "rhel-9.8.z")
    assert r'component = "comp\"name"' in jql
    assert 'fixVersion in ("rhel-9.8", "rhel-9.8.z")' in jql


def test_sibling_analysis_prompt_accepts_inherited_fix_version():
    prompt = _build_sibling_analysis_prompt(
        candidate_key="RHEL-200",
        jira_issue="RHEL-100",
        package="git-lfs",
        dependency_component="golang",
        inherited_dependency_fix_version="rhel-9.9.z",
    )
    assert "Exception: a dependency issue with fixVersion rhel-9.9.z" in prompt
    # Rendered at the same indentation as the neighbouring list items.
    assert "\n   - Exception:" in prompt
    assert "\n   - If no matching CVE tracker found" in prompt


def test_sibling_analysis_prompt_without_inherited_fix_version():
    prompt = _build_sibling_analysis_prompt(
        candidate_key="RHEL-200",
        jira_issue="RHEL-100",
        package="git-lfs",
        dependency_component="golang",
    )
    assert "Exception:" not in prompt


def _mock_sibling_search(rhel_config):
    async def mock_run_tool(name, **_kwargs):
        if name == "search_jira_issues":
            return [{"key": "RHEL-200", "fields": {"summary": "CVE-2026-1234 git-lfs"}}]
        if name == "check_cve_triage_eligibility":
            return {"is_cve": True, "eligibility": "immediately", "reason": "ok"}
        raise AssertionError(f"unexpected tool call {name}")

    async def mock_load_rhel_config():
        if isinstance(rhel_config, Exception):
            raise rhel_config
        return rhel_config

    flexmock(rebuild_consolidation).should_receive("run_tool").replace_with(mock_run_tool)
    flexmock(rebuild_consolidation).should_receive("load_rhel_config").replace_with(mock_load_rhel_config)


def _rebuild_data():
    return RebuildData(
        package="git-lfs",
        jira_issue="RHEL-100",
        fix_version="rhel-9.10",
        dependency_component="golang",
    )


@pytest.mark.asyncio
async def test_find_rebuild_siblings_passes_inherited_fix_version():
    _mock_sibling_search({"current_y_streams": {"9": "rhel-9.10"}})
    captured = {}

    def mock_build_prompt(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after the prompt inputs are captured")

    flexmock(rebuild_consolidation).should_receive("ReasoningAgent").and_return(flexmock())
    flexmock(rebuild_consolidation).should_receive("get_chat_model").and_return(flexmock())
    flexmock(rebuild_consolidation).should_receive("_build_sibling_analysis_prompt").replace_with(
        mock_build_prompt
    )

    consolidated, summary = await find_rebuild_siblings("RHEL-100", _rebuild_data(), available_tools=[])

    assert captured["inherited_dependency_fix_version"] == "rhel-9.9.z"
    assert consolidated == []
    assert "RHEL-200 — excluded (analysis failed)" in summary


@pytest.mark.asyncio
async def test_find_rebuild_siblings_skips_consolidation_when_config_fails():
    _mock_sibling_search(RuntimeError("config unavailable"))
    flexmock(rebuild_consolidation).should_receive("_build_sibling_analysis_prompt").never()

    assert await find_rebuild_siblings("RHEL-100", _rebuild_data(), available_tools=[]) == ([], "")
