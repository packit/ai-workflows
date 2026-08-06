from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ymir.agents.triage_agent import (
    TriageState,
    _build_reproducer_input,
    _map_version_to_module_branch,
    _should_update_jira,
    determine_target_branch,
)
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


async def _capture_process_task(main_fn):
    """Run main() in queue mode, capture the process_task closure it registers."""
    captured = {}

    async def fake_run_task_loop(_redis, _queues, process_fn, **_kw):
        captured["process_task"] = process_fn

    with (
        patch("ymir.agents.triage_agent.init_sentry"),
        patch("ymir.agents.triage_agent.configure_logging"),
        patch("ymir.agents.triage_agent.resolve_chat_model_override"),
        patch("ymir.agents.triage_agent.setup_observability", return_value=MagicMock()),
        patch("ymir.agents.triage_agent.run_task_loop", side_effect=fake_run_task_loop),
        patch("ymir.agents.triage_agent.redis_client") as mock_redis_ctx,
        patch.dict(
            "os.environ",
            {
                "COLLECTOR_ENDPOINT": "http://localhost:6006",
                "REDIS_URL": "redis://localhost",
                "MCP_GATEWAY_URL": "http://mcp-gateway:8000/sse",
                "DRY_RUN": "true",
            },
            clear=False,
        ),
    ):
        mock_redis_ctx.return_value.__aenter__ = AsyncMock()
        mock_redis_ctx.return_value.__aexit__ = AsyncMock()
        await main_fn()

    return captured["process_task"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Closed", "Done"])
async def test_process_task_skips_closed_issues(status):
    """Closed/Done issues are skipped without calling run_workflow."""
    from ymir.agents.triage_agent import main

    process_task = await _capture_process_task(main)

    with (
        patch("ymir.agents.triage_agent.issue_lock", side_effect=_always_acquired_lock),
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([], status),
        ),
        patch("ymir.agents.triage_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
    ):
        await process_task(_make_payload())

    mock_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_task_skips_closed_user_triggered_with_cleanup():
    """User-triggered run on a closed issue removes ymir_todo and posts ack."""
    from ymir.agents.triage_agent import main

    process_task = await _capture_process_task(main)

    with (
        patch("ymir.agents.triage_agent.issue_lock", side_effect=_always_acquired_lock),
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=(["ymir_todo"], "Closed"),
        ),
        patch("ymir.agents.triage_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock) as mock_labels,
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock) as mock_ack,
    ):
        await process_task(_make_payload(user_triggered=True))

    mock_workflow.assert_not_awaited()
    mock_labels.assert_awaited_once()
    _, kwargs = mock_labels.call_args
    assert kwargs["labels_to_remove"] == ["ymir_todo"]
    assert kwargs["dry_run"] is True
    mock_ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_task_proceeds_for_open_issues():
    """An open issue (e.g. New) is not blocked by the closed-issue check."""
    from ymir.agents.triage_agent import main

    process_task = await _capture_process_task(main)

    with (
        patch("ymir.agents.triage_agent.issue_lock", side_effect=_always_acquired_lock),
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([], "New"),
        ),
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock),
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock),
        patch("ymir.agents.triage_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
    ):
        await process_task(_make_payload())

    mock_workflow.assert_awaited_once()


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
    with patch(
        "ymir.agents.triage_agent.is_older_zstream",
        new_callable=AsyncMock,
        return_value=False,
    ):
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
    with patch(
        "ymir.agents.triage_agent.is_older_zstream",
        new_callable=AsyncMock,
        return_value=False,
    ):
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
    with patch(
        "ymir.agents.triage_agent.is_older_zstream",
        new_callable=AsyncMock,
        return_value=True,
    ):
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
    with patch(
        "ymir.agents.triage_agent._map_version_to_branch",
        new_callable=AsyncMock,
        return_value="rhel-10.2",
    ):
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
async def test_process_task_drops_duplicate_when_locked():
    """When the per-issue lock is already held, process_task silently drops the task."""
    from ymir.agents.triage_agent import main

    process_task = await _capture_process_task(main)

    with (
        patch("ymir.agents.triage_agent.issue_lock", side_effect=_lock_already_held),
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
        ) as mock_metadata,
        patch("ymir.agents.triage_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
    ):
        await process_task(_make_payload())

    mock_metadata.assert_not_awaited()
    mock_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_task_acquires_lock_and_proceeds():
    """When the lock is available, process_task proceeds to call run_workflow."""
    from ymir.agents.triage_agent import main

    process_task = await _capture_process_task(main)

    with (
        patch("ymir.agents.triage_agent.issue_lock", side_effect=_always_acquired_lock),
        patch(
            "ymir.agents.tasks.get_jira_issue_metadata",
            new_callable=AsyncMock,
            return_value=([], "New"),
        ),
        patch("ymir.agents.tasks.set_jira_labels", new_callable=AsyncMock),
        patch("ymir.agents.tasks.post_user_ack_once", new_callable=AsyncMock),
        patch("ymir.agents.triage_agent.run_workflow", new_callable=AsyncMock) as mock_workflow,
    ):
        await process_task(_make_payload())

    mock_workflow.assert_awaited_once()


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
async def test_pending_dependencies_maps_to_postponed_y_stream():
    """PENDING_DEPENDENCIES eligibility must produce POSTPONED_Y_STREAM, not
    POSTPONED_DEPENDENCY.  The former is swept by YStreamSweep; the latter by
    DependencySweep (rebuild waiting for a component's fixed build).  Mixing
    them up silently deadlocks one of the two sweep paths."""
    from ymir.agents.triage_agent import run_workflow

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

    with (
        patch("ymir.agents.triage_agent.mcp_tools", side_effect=_mock_mcp_tools),
        patch(
            "ymir.agents.triage_agent.run_tool",
            new_callable=AsyncMock,
            return_value=eligibility_result.model_dump(),
        ),
        patch("ymir.agents.triage_agent.get_mock_local_tool_env", return_value=None),
        patch.dict(
            "os.environ",
            {"GIT_REPO_BASEPATH": "/tmp", "MCP_GATEWAY_URL": "http://localhost"},
            clear=False,
        ),
    ):
        state = await run_workflow(
            "RHEL-99999",
            dry_run=True,
            triage_agent_factory=MagicMock(),
        )

    assert state.triage_result.resolution == Resolution.POSTPONED_Y_STREAM
    assert state.triage_result.data.pending_issues == pending_issues
