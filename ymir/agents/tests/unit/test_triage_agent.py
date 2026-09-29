from contextlib import asynccontextmanager

import pytest
from flexmock import flexmock

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
    _map_version_to_module_branch,
    _postponed_comment_exists,
    _should_update_jira,
    determine_target_branch,
    main,
    render_prompt,
    run_workflow,
)
from ymir.common.constants import YMIR_COMMENT_MARKER, JiraLabels
from ymir.common.models import (
    ApplicabilityResult,
    BackportData,
    ConsolidatedIssue,
    CVEEligibilityResult,
    NotAffectedData,
    PostponedData,
    RebaseData,
    RebuildData,
    Resolution,
    Task,
    TriageEligibility,
    TriageInputSchema,
    TriageOutputSchema,
)
from ymir.common.utils import FIXED_IN_BUILD_CUSTOM_FIELD
from ymir.common.version_utils import extract_downstream_package, is_modular, parse_module_stream


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.REBASE,
        Resolution.BACKPORT,
        Resolution.REBUILD,
        Resolution.NOT_AFFECTED,
        Resolution.POSTPONED_DEPENDENCY,
        Resolution.POSTPONED_Y_STREAM,
        Resolution.POSTPONED_NO_PATCH,
        Resolution.POSTPONED_PR_PENDING,
        Resolution.OPEN_ENDED_ANALYSIS,
        Resolution.CLARIFICATION_NEEDED,
    ],
)
def test_user_triggered_run_posts_normal_results(resolution):
    """A maintainer-triggered run gets a normal result comment."""
    assert _should_update_jira(resolution=resolution, user_triggered=True) is True


def test_triage_agent_exposes_authenticated_github_patch_tool(monkeypatch):
    captured = {}

    def reasoning_agent_factory(**kwargs):
        captured.update(kwargs)
        return flexmock()

    monkeypatch.setattr(t_agent, "ReasoningAgent", reasoning_agent_factory)
    monkeypatch.setattr(t_agent, "get_chat_model", lambda: None)
    monkeypatch.setattr(t_agent, "is_reasoning_enabled", lambda: False)
    monkeypatch.setattr(t_agent, "get_tool_call_checker_config", lambda: None)

    gateway_tools = [
        flexmock(name="get_patch_from_url"),
        flexmock(name="get_github_patch"),
        flexmock(name="get_github_pull_request"),
        flexmock(name="get_github_compare"),
    ]
    t_agent.create_triage_agent(gateway_tools)

    tool_names = {tool.name for tool in captured["tools"]}
    assert {
        "get_patch_from_url",
        "get_github_patch",
        "get_github_pull_request",
        "get_github_compare",
    } <= tool_names


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.REBASE,
        Resolution.BACKPORT,
        Resolution.REBUILD,
    ],
)
def test_non_user_triggered_skips_comment_when_mr_will_be_opened(resolution):
    """Without ymir_todo, runs do not comment when an MR will be opened —
    the MR itself is the user-visible artifact."""
    assert _should_update_jira(resolution=resolution, user_triggered=False) is False


def test_non_user_triggered_posts_manual_branch_creation_hold():
    """A manual branch hold must be visible even when the resolution would normally be silent."""
    assert (
        _should_update_jira(
            resolution=Resolution.REBASE,
            user_triggered=False,
            requires_manual_branch_creation=True,
        )
        is True
    )


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.NOT_AFFECTED,
        Resolution.POSTPONED_DEPENDENCY,
        Resolution.POSTPONED_Y_STREAM,
        Resolution.POSTPONED_NO_PATCH,
        Resolution.POSTPONED_PR_PENDING,
        Resolution.OPEN_ENDED_ANALYSIS,
        Resolution.CLARIFICATION_NEEDED,
    ],
)
def test_non_user_triggered_still_posts_when_no_mr_will_open(resolution):
    """Resolutions that do not produce an MR must still post a comment —
    otherwise the result is invisible to the requester."""
    assert _should_update_jira(resolution=resolution, user_triggered=False) is True


def test_error_deferred_to_terminal_retry_path():
    """ERROR is dispatched to retry() and commented once after retries are exhausted."""
    assert _should_update_jira(resolution=Resolution.ERROR, user_triggered=False) is False
    assert _should_update_jira(resolution=Resolution.ERROR, user_triggered=True) is False


def _make_payload(issue: str = "RHEL-99999", user_triggered: bool = False) -> bytes:
    task = Task.from_issue(issue, user_triggered=user_triggered)
    return task.model_dump_json().encode()


@asynccontextmanager
async def _always_acquired_lock(*_args, **_kwargs):
    yield "test-lock-token"


@pytest.fixture
def _mock_env_vars_redis(monkeypatch):
    monkeypatch.setenv("COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://mcp-gateway:8000/sse")
    monkeypatch.setenv("DRY_RUN", "true")


@pytest.fixture
def _mock_env_vars(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://localhost")
    monkeypatch.setenv("GIT_REPO_BASEPATH", "/tmp")


async def _async_noop(*_args, **_kwargs):
    pass


async def _capture_process_task(main_fn, redis_mock=None):
    """Run main() in queue mode, capture the process_task closure it registers."""
    captured = {}

    async def _mock_run_task_loop(_redis, _queues, process_fn, **_kw):
        captured["process_task"] = process_fn

    @asynccontextmanager
    async def _mock_redis_client(*_args, **_kwargs):
        client = redis_mock or flexmock()
        if redis_mock is None:
            client.should_receive("lpush").replace_with(_async_noop)
            client.should_receive("incr").replace_with(_async_noop)
        yield client

    transaction = flexmock()
    transaction.should_receive("__enter__").and_return(None)
    transaction.should_receive("__exit__").and_return(False)

    span_processor = flexmock()
    span_processor.should_receive("start_transaction").and_return(transaction)

    flexmock(t_agent).should_receive("init_sentry")
    flexmock(t_agent).should_receive("configure_logging")
    flexmock(t_agent).should_receive("resolve_chat_model_override")
    flexmock(t_agent).should_receive("setup_observability").and_return(span_processor)
    flexmock(t_agent).should_receive("run_task_loop").replace_with(_mock_run_task_loop)
    flexmock(t_agent).should_receive("redis_client").replace_with(_mock_redis_client)

    await main_fn()

    return captured["process_task"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Closed", "Done"])
async def test_process_task_skips_closed_issues(status, _mock_env_vars_redis):
    """Closed/Done issues are skipped without calling run_workflow."""

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], status

    flexmock(t_agent).should_receive("issue_lock").replace_with(_always_acquired_lock)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(t_agent).should_receive("run_workflow").never()

    process_task = await _capture_process_task(main)
    await process_task(_make_payload())


@pytest.mark.asyncio
async def test_process_task_skips_closed_user_triggered_with_cleanup(_mock_env_vars_redis):
    """User-triggered run on a closed issue removes ymir_todo and posts ack."""

    async def _mock_jira_metadata(*_args, **_kwargs):
        return ["ymir_todo"], "Closed"

    calls = []

    async def _mock_jira_labels(*_args, **_kwargs):
        calls.append((_args, _kwargs))

    flexmock(t_agent).should_receive("issue_lock").replace_with(_always_acquired_lock)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(t_agent).should_receive("run_workflow").never()
    flexmock(agent_tasks).should_receive("set_jira_labels").once().replace_with(_mock_jira_labels)
    flexmock(agent_tasks).should_receive("post_user_ack_once").once().replace_with(_async_noop)

    process_task = await _capture_process_task(main)
    await process_task(_make_payload(user_triggered=True))

    _, kwargs = calls[0]
    assert kwargs["labels_to_remove"] == ["ymir_todo"]
    assert kwargs["dry_run"] is True


@pytest.mark.asyncio
async def test_process_task_proceeds_for_open_issues(_mock_env_vars_redis):
    """An open issue (e.g. New) is not blocked by the closed-issue check."""

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    flexmock(t_agent).should_receive("issue_lock").replace_with(_always_acquired_lock)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(agent_tasks).should_receive("set_jira_labels").replace_with(_async_noop)
    flexmock(agent_tasks).should_receive("post_user_ack_once").replace_with(_async_noop)
    flexmock(t_agent).should_receive("run_workflow").once().replace_with(_async_noop)

    process_task = await _capture_process_task(main)
    await process_task(_make_payload())


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


@asynccontextmanager
async def _lock_already_held(*_args, **_kwargs):
    yield None


@pytest.mark.asyncio
async def test_process_task_drops_duplicate_when_locked(_mock_env_vars_redis):
    """When the per-issue lock is already held, process_task silently drops the task."""
    flexmock(t_agent).should_receive("issue_lock").replace_with(_lock_already_held)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").never()
    flexmock(t_agent).should_receive("run_workflow").never()

    process_task = await _capture_process_task(main)
    await process_task(_make_payload())


@pytest.mark.asyncio
async def test_process_task_acquires_lock_and_proceeds(_mock_env_vars_redis):
    """When the lock is available, process_task proceeds to call run_workflow."""

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    flexmock(t_agent).should_receive("issue_lock").replace_with(_always_acquired_lock)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(agent_tasks).should_receive("set_jira_labels").replace_with(_async_noop)
    flexmock(agent_tasks).should_receive("post_user_ack_once").replace_with(_async_noop)
    flexmock(t_agent).should_receive("run_workflow").once().replace_with(_async_noop)

    process_task = await _capture_process_task(main)
    await process_task(_make_payload())


@pytest.mark.asyncio
async def test_triage_retry_is_queued_after_issue_lock_release(_mock_env_vars_redis):
    events = []
    client = flexmock()

    async def _record_push(queue, payload):
        events.append(("push", queue, Task.model_validate_json(payload).attempts))

    @asynccontextmanager
    async def _record_lock(*_args, **_kwargs):
        events.append("lock_acquired")
        try:
            yield "test-lock-token"
        finally:
            events.append("lock_released")

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    async def _failed_workflow(*_args, **_kwargs):
        raise RuntimeError("triage failed")

    client.should_receive("lpush").replace_with(_record_push)
    flexmock(t_agent).should_receive("issue_lock").replace_with(_record_lock)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(agent_tasks).should_receive("set_jira_labels").replace_with(_async_noop)
    flexmock(agent_tasks).should_receive("post_user_ack_once").replace_with(_async_noop)
    flexmock(t_agent).should_receive("run_workflow").replace_with(_failed_workflow)

    process_task = await _capture_process_task(main, client)
    await process_task(_make_payload())

    assert events == ["lock_acquired", "lock_released", ("push", "triage_queue", 1)]


@pytest.mark.asyncio
async def test_triage_terminal_error_clears_manual_branch_hold(_mock_env_vars_redis, monkeypatch):
    monkeypatch.setenv("MAX_RETRIES", "1")
    labels = []
    client = flexmock()

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    async def _record_labels(**kwargs):
        labels.append(kwargs)

    async def _failed_workflow(*_args, **_kwargs):
        raise RuntimeError("triage failed")

    async def _next_error_id(*_args, **_kwargs):
        return 1

    client.should_receive("incr").replace_with(_next_error_id)
    client.should_receive("lpush").replace_with(_async_noop)
    flexmock(t_agent).should_receive("issue_lock").replace_with(_always_acquired_lock)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(agent_tasks).should_receive("set_jira_labels").replace_with(_record_labels)
    flexmock(agent_tasks).should_receive("post_user_ack_once").replace_with(_async_noop)
    flexmock(t_agent).should_receive("run_workflow").replace_with(_failed_workflow)

    process_task = await _capture_process_task(main, client)
    await process_task(_make_payload())

    assert labels[-1]["labels_to_add"] == [JiraLabels.TRIAGE_ERRORED.value]
    assert JiraLabels.MANUAL_BRANCH_NEEDED.value in labels[-1]["labels_to_remove"]


@pytest.mark.asyncio
async def test_held_primary_is_not_labeled_twice(_mock_env_vars_redis, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("TRIAGE_ENQUEUE_REPRODUCER", "false")
    state = TriageState(
        jira_issue="RHEL-99999",
        target_branch="rhel-10.3",
        target_branch_exists=False,
        automatic_branch_creation_disabled=True,
        triage_result=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="bash", jira_issue="RHEL-99999", patch_urls=[], justification="Test hold"
            ),
        ),
    )
    labels = []

    async def _record_labels(**kwargs):
        labels.append(kwargs)

    async def _mock_jira_metadata(*_args, **_kwargs):
        return [], "New"

    async def _mock_workflow(*_args, **_kwargs):
        return state

    flexmock(t_agent).should_receive("issue_lock").replace_with(_always_acquired_lock)
    flexmock(agent_tasks).should_receive("get_jira_issue_metadata").replace_with(_mock_jira_metadata)
    flexmock(agent_tasks).should_receive("set_jira_labels").replace_with(_record_labels)
    flexmock(agent_tasks).should_receive("post_user_ack_once").replace_with(_async_noop)
    flexmock(t_agent).should_receive("run_workflow").replace_with(_mock_workflow)

    process_task = await _capture_process_task(main)
    await process_task(_make_payload())

    assert len(labels) == 1
    assert labels[0]["labels_to_add"] == [JiraLabels.TRIAGE_IN_PROGRESS.value]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "ready",
        "direct",
        "sibling",
        "waiting",
        "consolidated",
        "comment_error",
    ],
)
@pytest.mark.parametrize(
    ("initial_resolution", "is_affected", "in_buildroot", "expected_resolution", "expect_hold"),
    [
        (Resolution.BACKPORT, True, True, Resolution.BACKPORT, True),
        (Resolution.REBASE, True, True, Resolution.REBASE, True),
        (Resolution.REBUILD, True, True, Resolution.REBUILD, True),
        (Resolution.REBUILD, False, True, Resolution.NOT_AFFECTED, False),
        (Resolution.REBUILD, True, False, Resolution.POSTPONED_DEPENDENCY, False),
    ],
)
async def test_branch_creation_notice_uses_final_resolution(
    scenario,
    initial_resolution,
    is_affected,
    in_buildroot,
    expected_resolution,
    expect_hold,
    _mock_env_vars,
    monkeypatch,
    tmp_path,
):
    """Applicability and buildroot decisions can supersede the manual branch hold."""
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    common_data = {
        "package": "bash",
        "jira_issue": "RHEL-99999",
        "cve_id": "CVE-2025-1234",
        "fix_version": "rhel-10.3.z",
    }
    if initial_resolution == Resolution.BACKPORT:
        data = BackportData(**common_data, patch_urls=[], justification="Apply upstream fix")
    elif initial_resolution == Resolution.REBASE:
        data = RebaseData(**common_data, version="5.3")
    else:
        data = RebuildData(**common_data, dependency_issue="RHEL-1", dependency_component="openssl")
    output = TriageOutputSchema(resolution=initial_resolution, data=data)
    eligibility = CVEEligibilityResult(
        is_cve=True, eligibility=TriageEligibility.IMMEDIATELY, reason="Eligible"
    )
    applicability = ApplicabilityResult(is_affected=is_affected, explanation="Source analysis result")

    @asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield []

    async def _mock_run_tool(name, **kwargs):
        if name == "check_cve_triage_eligibility":
            return eligibility.model_dump()
        if name == "verify_issue_author":
            return True
        if name == "get_internal_rhel_branches":
            return []
        if name == "get_jira_details":
            fields = {FIXED_IN_BUILD_CUSTOM_FIELD: "openssl-3.0-1.el10"}
            if scenario == "sibling":
                fields["comment"] = {
                    "comments": [{"body": "Queued for triage as potential sibling of RHEL-100"}]
                }
            return {"fields": fields}
        raise AssertionError(f"Unexpected tool: {name}")

    async def _mock_rules(name, **kwargs):
        assert name == "get_maintainer_rules"
        assert kwargs["file_path"] == "ymir.yaml"
        return "branch_creation:\n  automatic: false\n"

    def _async_return(value):
        async def _return(*_args, **_kwargs):
            return value

        return _return

    triage_agent = flexmock()
    triage_agent.should_receive("run").replace_with(
        _async_return(flexmock(last_message=flexmock(text=output.model_dump_json())))
    )
    applicability_agent = flexmock()
    applicability_agent.should_receive("run").replace_with(
        _async_return(flexmock(last_message=flexmock(text=applicability.model_dump_json())))
    )
    flexmock(t_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    monkeypatch.setattr(agent_tasks, "mcp_tools", _mock_mcp_tools)
    flexmock(t_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_rules)
    monkeypatch.setattr(t_agent, "get_mock_local_tool_env", lambda *_: None)
    monkeypatch.setattr(t_agent, "get_agent_execution_config", dict)
    monkeypatch.setattr(t_agent, "render_template", lambda *_: "output format")
    monkeypatch.setattr(t_agent, "render_prompt", _async_return("triage prompt"))
    monkeypatch.setattr(t_agent, "load_rhel_config", _async_return({}))
    monkeypatch.setattr(t_agent, "determine_target_branch", _async_return(("rhel-10.3", None)))
    monkeypatch.setattr(t_agent, "is_older_zstream", _async_return(False))
    monkeypatch.setattr(
        agent_tasks, "clone_and_prep_sources", _async_return((tmp_path, tmp_path, True, None))
    )
    monkeypatch.setattr(t_agent, "create_applicability_agent", lambda *_: applicability_agent)
    monkeypatch.setattr(t_agent, "check_build_in_buildroot", _async_return(in_buildroot))
    siblings = [ConsolidatedIssue(issue_key="RHEL-200")] if scenario == "consolidated" else []
    monkeypatch.setattr(t_agent, "find_rebuild_siblings", _async_return((siblings, None)))
    monkeypatch.setattr(agent_tasks, "get_jira_issue_metadata", _async_return(([], "New")))
    monkeypatch.setattr(
        t_agent, "queue_siblings_for_triage", _async_return(2 if scenario == "waiting" else 0)
    )
    labels = []
    comments = []

    async def _record_labels(**kwargs):
        labels.append(kwargs)

    async def _record_comment(**kwargs):
        comments.append(kwargs)
        if scenario == "comment_error" and expect_hold:
            raise RuntimeError("Jira comments unavailable")

    monkeypatch.setattr(agent_tasks, "set_jira_labels", _record_labels)
    monkeypatch.setattr(agent_tasks, "comment_in_jira", _record_comment)

    state = await run_workflow(
        "RHEL-99999",
        dry_run=False,
        triage_agent_factory=lambda *_: triage_agent,
        auto_chain=scenario != "direct",
    )

    assert state.automatic_branch_creation_disabled is True
    assert state.triage_result.resolution == expected_resolution
    if initial_resolution == Resolution.REBASE and scenario in ("sibling", "waiting"):
        assert not state.hold_for_manual_branch_creation
        assert not labels
        assert not comments
        return
    held_issues = {"RHEL-99999"} if expect_hold else set()
    if expect_hold and scenario == "consolidated" and initial_resolution == Resolution.REBUILD:
        held_issues.add("RHEL-200")
    assert {call["jira_issue"] for call in labels} == held_issues
    for call in labels:
        assert JiraLabels.MANUAL_BRANCH_NEEDED.value in call["labels_to_add"]
        assert call["critical"] is True
    assert len(comments) == (len(held_issues) or 1)
    text = comments[-1]["comment_text"]
    assert ("Automatic branch creation is disabled" in text) is expect_hold
    assert ("create the branch manually" in text) is expect_hold
    if expect_hold:
        assert all("label to RHEL-99999" in call["comment_text"] for call in comments)


@pytest.mark.asyncio
async def test_pending_dependencies_maps_to_postponed_y_stream(_mock_env_vars):
    """PENDING_DEPENDENCIES eligibility must produce POSTPONED_Y_STREAM, not
    POSTPONED_DEPENDENCY.  The former is swept by YStreamSweep; the latter by
    DependencySweep (rebuild waiting for a component's fixed build).  Mixing
    them up silently deadlocks one of the two sweep paths."""
    pending_issues = ["RHEL-99998"]
    eligibility_result = CVEEligibilityResult(
        is_cve=True,
        eligibility=TriageEligibility.PENDING_DEPENDENCIES,
        reason="Waiting for Z-stream clones to ship",
        pending_zstream_issues=pending_issues,
    )

    @asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield []

    async def _mock_run_tool(*_args, **_kwargs):
        return eligibility_result.model_dump()

    flexmock(t_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(t_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(t_agent).should_receive("get_mock_local_tool_env").and_return(None)

    state = await run_workflow(
        "RHEL-99999",
        dry_run=True,
        triage_agent_factory=lambda *_args, **_kwargs: flexmock(),
    )

    assert state.triage_result.resolution == Resolution.POSTPONED_Y_STREAM
    assert state.triage_result.data.pending_issues == pending_issues


@pytest.mark.asyncio
async def test_pr_pending_without_blocker_reference_raises(_mock_env_vars):
    """A postponed_pr_pending resolution with no blocker_references URL is not
    sweepable (PRPendingSweep has no MR/PR to poll), so run_triage_analysis must
    raise rather than silently produce a permanently-stuck issue. The raise is
    caught by the workflow's outer handler and routed through retry()."""
    eligibility_result = CVEEligibilityResult(
        is_cve=True,
        eligibility=TriageEligibility.IMMEDIATELY,
        reason="Eligible for immediate triage",
    )

    # LLM output: postponed_pr_pending but blocker_references omitted.
    llm_json = (
        '{"resolution": "postponed_pr_pending", "data": {'
        '"summary": "Fix pending in upstream MR; waiting for merge", '
        '"pending_issues": ["RHEL-1"], "jira_issue": "RHEL-99999"}}'
    )

    async def _mock_agent_run(*_args, **_kwargs):
        return flexmock(last_message=flexmock(text=llm_json))

    def _mock_factory(*_args, **_kwargs):
        return agent

    @asynccontextmanager
    async def _mock_mcp_tools(*_args, **_kwargs):
        yield []

    async def _mock_run_tool(*_args, **_kwargs):
        return eligibility_result.model_dump()

    async def _mock_prompt(*_args, **_kwargs):
        return "prompt"

    agent = flexmock()
    agent.should_receive("run").replace_with(_mock_agent_run)
    flexmock(t_agent).should_receive("mcp_tools").replace_with(_mock_mcp_tools)
    flexmock(t_agent).should_receive("run_tool").replace_with(_mock_run_tool)
    flexmock(t_agent).should_receive("get_mock_local_tool_env").and_return(None)
    flexmock(t_agent).should_receive("render_template").and_return("output format")
    flexmock(t_agent).should_receive("get_agent_execution_config").and_return({})
    flexmock(t_agent).should_receive("render_prompt").replace_with(_mock_prompt)

    with pytest.raises(Exception) as excinfo:
        await run_workflow(
            "RHEL-99999",
            dry_run=True,
            triage_agent_factory=_mock_factory,
        )

    # The beeai Workflow wraps a node's exception in a FrameworkError, chaining
    # the original via __cause__. The outer handler in _process_triage_locked
    # catches it (except Exception) and routes to retry(); here we assert the
    # guard's ValueError is what propagated.
    chain = []
    err = excinfo.value
    while err is not None:
        chain.append(err)
        err = err.__cause__
    assert any(isinstance(e, ValueError) and "postponed_pr_pending" in str(e) for e in chain), (
        f"expected a chained ValueError about postponed_pr_pending, got: {chain!r}"
    )


def _adf_paragraph(*text_nodes: dict) -> dict:
    return {"type": "paragraph", "content": list(text_nodes)}


def _adf_ymir_postponed_body(resolution: Resolution, summary: str = "no upstream fix yet") -> dict:
    """Build an ADF comment body as Jira REST v3 (get_jira_details) returns it.

    Crucially this drops the ``*bold*`` wiki markup the agent posts: the
    ``*Resolution*`` label becomes a strong-marked text node ("Resolution",
    no asterisks) and the value lands in a separate text node. This is what
    ``extract_text_from_adf`` actually sees in production, so the guard must
    match on the marker + snake_case value token, not the rendered
    ``*Resolution*:`` string.
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [
            _adf_paragraph({"type": "text", "text": "Output from Ymir Triage Agent: "}),
            _adf_paragraph(
                {"type": "text", "text": "Resolution", "marks": [{"type": "strong"}]},
                {"type": "text", "text": f": {resolution.value}"},
            ),
            _adf_paragraph(
                {"type": "text", "text": "Summary", "marks": [{"type": "strong"}]},
                {"type": "text", "text": f": {summary}"},
            ),
        ],
    }


def _details_with_comments(*bodies) -> dict:
    """Wrap comment bodies (ADF dicts or plain strings) in the get_jira_details shape."""
    return {"fields": {"comment": {"comments": [{"body": b} for b in bodies]}}}


@pytest.mark.asyncio
async def test_postponed_comment_exists_true_same_resolution():
    """Returns True when a prior Ymir ADF comment records the same resolution."""

    async def _mock_run_tool(*_args, **_kwargs):
        return _details_with_comments(
            "some unrelated human comment",
            _adf_ymir_postponed_body(Resolution.POSTPONED_NO_PATCH),
        )

    flexmock(t_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    assert await _postponed_comment_exists("RHEL-99999", Resolution.POSTPONED_NO_PATCH, []) is True


@pytest.mark.asyncio
async def test_postponed_comment_exists_false_different_resolution():
    """A prior Ymir comment for a DIFFERENT resolution does not suppress."""

    async def _mock_run_tool(*_args, **_kwargs):
        return _details_with_comments(_adf_ymir_postponed_body(Resolution.POSTPONED_PR_PENDING))

    flexmock(t_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    assert await _postponed_comment_exists("RHEL-99999", Resolution.POSTPONED_NO_PATCH, []) is False


@pytest.mark.asyncio
async def test_postponed_comment_exists_false_without_marker():
    """The resolution value without the Ymir marker (e.g. a human quote) is ignored."""

    async def _mock_run_tool(*_args, **_kwargs):
        return _details_with_comments(
            {
                "type": "doc",
                "version": 1,
                "content": [
                    _adf_paragraph(
                        {"type": "text", "text": "I think this should be postponed_no_patch, agreed?"}
                    )
                ],
            }
        )

    flexmock(t_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    assert await _postponed_comment_exists("RHEL-99999", Resolution.POSTPONED_NO_PATCH, []) is False


@pytest.mark.asyncio
async def test_postponed_comment_exists_false_no_comments():
    """No comments at all -> nothing to deduplicate against."""

    async def _mock_run_tool(*_args, **_kwargs):
        return _details_with_comments()

    flexmock(t_agent).should_receive("run_tool").replace_with(_mock_run_tool)

    assert await _postponed_comment_exists("RHEL-99999", Resolution.POSTPONED_NO_PATCH, []) is False


def test_ymir_comment_marker_triage():
    """The shared marker template renders the exact string the sweep matches."""
    assert YMIR_COMMENT_MARKER.substitute(AGENT_TYPE="Triage") == "Output from Ymir Triage Agent"


def test_jira_comment_template_output_unchanged():
    """Rebuilding JIRA_COMMENT_TEMPLATE from the shared marker keeps output identical."""
    rendered = JIRA_COMMENT_TEMPLATE.substitute(AGENT_TYPE="Triage", JIRA_COMMENT="hello")
    assert rendered.startswith("Output from Ymir Triage Agent: \n\nhello\n\n")
