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
    run_workflow,
)
from ymir.common.constants import YMIR_COMMENT_MARKER
from ymir.common.models import (
    BackportData,
    CVEEligibilityResult,
    NotAffectedData,
    PostponedData,
    RebaseData,
    RebuildData,
    Resolution,
    Task,
    TriageEligibility,
    TriageOutputSchema,
)
from ymir.common.version_utils import is_modular, parse_module_stream


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
        Resolution.ERROR,
    ],
)
def test_user_triggered_always_posts(resolution):
    """A maintainer-triggered run always gets a comment, regardless of resolution."""
    assert _should_update_jira(resolution=resolution, user_triggered=True) is True


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


def test_non_user_triggered_error_does_not_post():
    """ERROR is handled by separate error-path machinery, not this helper."""
    assert _should_update_jira(resolution=Resolution.ERROR, user_triggered=False) is False


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


async def _capture_process_task(main_fn):
    """Run main() in queue mode, capture the process_task closure it registers."""
    captured = {}

    async def _mock_run_task_loop(_redis, _queues, process_fn, **_kw):
        captured["process_task"] = process_fn

    @asynccontextmanager
    async def _mock_redis_client(*_args, **_kwargs):
        redis_mock = flexmock()
        redis_mock.should_receive("lpush").replace_with(_async_noop)
        redis_mock.should_receive("incr").replace_with(_async_noop)
        yield redis_mock

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


@pytest.mark.asyncio
async def test_determine_target_branch_modular_internal_fix_uses_rhel():
    async def _older_zstream_false(*_args, **_kwargs):
        return False

    flexmock(t_agent).should_receive("is_older_zstream").replace_with(_older_zstream_false)

    branch, namespace = await determine_target_branch(
        _cve_eligibility(needs_internal_fix=True),
        _modular_backport_data(),
        jira_summary=_MODULAR_SUMMARY,
        downstream_component="squid",
    )
    assert branch == "stream-squid-4-rhel-8.10.0"
    assert namespace == "rhel"


@pytest.mark.asyncio
async def test_determine_target_branch_modular_cs_eligible_uses_centos_stream():
    async def _older_zstream_false(*_args, **_kwargs):
        return False

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
    async def _older_zstream_true(*_args, **_kwargs):
        return True

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
