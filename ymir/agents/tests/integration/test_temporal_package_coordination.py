"""Verify package-stream gating against local Temporal and mocked Jira/GitLab."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from flexmock import flexmock
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from ymir.agents import temporal_package_coordination as coordination
from ymir.agents import temporal_triage_publication as publication
from ymir.agents.temporal_package_coordination import (
    WAITING,
    CoordinatedTriage,
    JiraMember,
    PackageStreamActivities,
    PackageStreamWorkflow,
    PlannedRelease,
    ValidatedFix,
    plan_package_stream,
    predecessors,
)
from ymir.agents.temporal_triage_analysis import TriageAnalysisRequest, TriageAnalysisResult
from ymir.agents.temporal_triage_publication import (
    PublicationInputs,
    TriagePublicationRequest,
    _task_handoff,
    package_stream,
)
from ymir.agents.triage_agent import TriageState
from ymir.common.constants import JiraLabels
from ymir.common.models import BackportData, RebaseData, RebuildData, Resolution, Task, TriageOutputSchema

ADDRESS = os.getenv("TEMPORAL_TEST_ADDRESS")
CVE = "CVE-2025-12345"
MR = "https://gitlab.com/redhat/rhel/rpms/sample/-/merge_requests/42"
PATCH = "https://github.com/upstream/sample/commit/" + "1" * 40 + ".patch"


def make_work(issue: str, *, package="sample") -> CoordinatedTriage:
    state = TriageState(
        jira_issue=issue,
        target_branch="rhel-9.8.0",
        triage_result=TriageOutputSchema(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package=package,
                jira_issue=issue,
                fix_version="rhel-9.8.z",
                justification="Fixture",
                patch_urls=["https://github.com/upstream/sample/pull/1.patch"],
            ),
        ),
    )
    request = TriagePublicationRequest(TriageAnalysisRequest(issue, {}, 1), uuid4().hex, dry_run=False)
    return CoordinatedTriage(
        package_stream(state),
        request,
        state,
        [_task_handoff(request, "backport_queue_c9s", state.model_dump(mode="json"), "backport")],
    )


def test_predecessor_follows_different_major_and_nearest_applicable_stream():
    low = JiraMember("RHEL-1", [], "New", ["rhel-9.8.z"], [CVE, "CVE-2025-45678"], [])
    next_fix = JiraMember("RHEL-2", [], "New", ["rhel-10.1.0.z"], [CVE], [])
    unaffected = JiraMember(
        "RHEL-3", [JiraLabels.TRIAGED_NOT_AFFECTED.value], "Done", ["rhel-10.1.z"], ["CVE-2025-45678"], []
    )
    top = JiraMember("RHEL-4", [], "New", ["rhel-10.2"], [CVE, "CVE-2025-45678"], [])
    assert predecessors(low, [next_fix, unaffected, top], "rhel-9.8") == {
        CVE: [next_fix],
        "CVE-2025-45678": [top],
    }


@pytest.mark.asyncio
async def test_validation_requires_authorized_record_and_current_mr_revision(monkeypatch):
    member = JiraMember("RHEL-2", [], "New", ["rhel-10.2"], [CVE], [])
    remote_sha = "a" * 40
    monkeypatch.setenv("TEMPORAL_TRIAGE_VALIDATION_AUTHORS", "trusted")
    record = {
        "schema": 1,
        "kind": "triage-fix-validation",
        "issue": member.issue,
        "stream": "rhel-10.2",
        "cves": [CVE],
        "merge_request": MR,
        "source_sha": "b" * 40,
        "resolution": "backport",
        "target_branch": "rhel-10.2.0",
        "patch_urls": [PATCH],
        "reproducer_required": False,
        "checks": dict.fromkeys(["build", "test", "coverage"], "https://example.test/result"),
    }

    async def revisions(path):
        assert path == "projects/redhat%2Frhel%2Frpms%2Fsample/merge_requests/42"
        return {"state": "opened", "target_branch": "rhel-10.2.0", "diff_refs": {"head_sha": remote_sha}}

    async def comments(issue, authors, kind):
        assert issue == member.issue and authors == {"trusted"} and kind == "triage-fix-validation"
        return [record]

    flexmock(coordination).should_receive("gitlab_api_get").replace_with(revisions)
    flexmock(coordination).should_receive("_jira_records").replace_with(comments)
    assert await coordination._validated_fix("sample", member, CVE) is None
    remote_sha = record["source_sha"]
    assert (await coordination._validated_fix("sample", member, CVE)).patch_urls == [PATCH]


@pytest.mark.asyncio
@pytest.mark.skipif(not ADDRESS, reason="Set TEMPORAL_TEST_ADDRESS to a local Temporal server")
async def test_package_workflow_waits_without_pod_and_releases_independent_work():
    token = uuid4().hex
    package = f"sample-{token}"
    issue = make_work("RHEL-1", package=package)
    independent = make_work("RHEL-3", package=package)
    low = JiraMember(issue.issue, [WAITING], "New", ["rhel-9.8.z"], [CVE], [])
    neighbor = JiraMember(independent.issue, [], "New", ["rhel-9.8.z"], [], [])
    higher = JiraMember("RHEL-2", [], "New", ["rhel-10.2"], [CVE], [])
    higher_ready = False
    released = []
    capacity = asyncio.Event()

    async def discover(_key, _pending, _snapshot):
        return [low, neighbor, higher]

    async def validate(_package, member, family):
        assert member == higher and family == CVE
        if higher_ready:
            return ValidatedFix(higher.issue, MR, "a" * 40, "c10s", "backport", [PATCH])
        return None

    flexmock(coordination).should_receive("_discover").replace_with(discover)
    flexmock(coordination).should_receive("_validated_fix").replace_with(validate)

    @activity.defn(name="submit_sibling_triage")
    async def submit_missing(issues: dict[str, str]) -> None:
        assert issues == {neighbor.issue: "initial"}
        capacity.set()

    @activity.defn(name="release_coordinated_triage")
    async def release(item: CoordinatedTriage, decision: PlannedRelease) -> bool:
        released.append((item.issue, decision))
        if item.issue == neighbor.issue:
            neighbor.labels.remove(WAITING)
            neighbor.labels.append(JiraLabels.TRIAGED_BACKPORT.value)
        return True

    client = await Client.connect(ADDRESS, plugins=[PydanticAIPlugin()])
    queue = f"package-test-{token}"
    submit = PackageStreamActivities(client, queue)
    worker_config = {
        "task_queue": queue,
        "workflows": [PackageStreamWorkflow],
        "activities": [plan_package_stream, submit_missing, release],
        "workflow_runner": SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                PackageStreamWorkflow.__module__
            )
        ),
    }
    handle = client.get_workflow_handle_for(PackageStreamWorkflow.run, issue.key.workflow_id)
    async with Worker(client, **worker_config):
        await submit.submit_package_coordination(issue)
        await asyncio.wait_for(capacity.wait(), 15)
        status = await handle.query(PackageStreamWorkflow.status)
        assert status["members"] == [issue.issue, neighbor.issue]
        assert neighbor.issue in status["blockers"][issue.issue]
        assert not released
        active = (await handle.describe()).run_id

    # The same workflow keeps its snapshot across replacement of the control worker.
    neighbor.labels.append(WAITING)
    async with Worker(client, **worker_config):
        await submit.submit_package_coordination(independent)
        async with asyncio.timeout(15):
            while not released:
                await asyncio.sleep(0.02)
        assert [key for key, _ in released] == [independent.issue]
        assert (await handle.describe()).run_id == active
        assert higher.issue in (await handle.query(PackageStreamWorkflow.status))["blockers"][issue.issue]
        higher_ready = True
        await submit.submit_package_coordination(issue)
        await asyncio.wait_for(handle.result(), 15)
        assert [key for key, _ in released] == [independent.issue, issue.issue]
        task = Task.model_validate_json(released[-1][1].handoffs[0].payload)
        assert task.metadata["triage_result"]["data"]["patch_urls"] == [PATCH]
        assert task.metadata["ystream_inheritance_disabled"] is True

    history = await handle.fetch_history()
    activities = {
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    }
    assert activities == {"plan_package_stream", "submit_sibling_triage", "release_coordinated_triage"}


def test_compatible_rebases_use_one_legacy_handoff():
    primary, sibling = make_work("RHEL-1"), make_work("RHEL-2")
    for item in (primary, sibling):
        item.state.triage_result = TriageOutputSchema(
            resolution=Resolution.REBASE,
            data=RebaseData(package="sample", version="2.0", jira_issue=item.issue, fix_version="rhel-9.8.z"),
        )
        item.handoffs = [
            _task_handoff(item.request, "rebase_queue_c9s", item.state.model_dump(mode="json"), "rebase")
        ]
    ready = {item.issue: PlannedRelease(item.handoffs.copy(), []) for item in (primary, sibling)}
    coordination.combine_compatible_handoffs([sibling, primary], ready)
    assert list(ready) == [primary.issue]
    decision = ready[primary.issue]
    assert [item.issue for item in decision.siblings] == [sibling.issue]
    [handoff] = decision.handoffs
    task = Task.model_validate_json(handoff.payload)
    data = TriageState.model_validate(task.metadata).triage_result.data
    assert data.all_jira_issues == [primary.issue, sibling.issue]


@pytest.mark.asyncio
async def test_rebuild_dependency_is_checked_only_in_shared_planner():
    work = make_work("RHEL-1")
    work.state.triage_result = TriageOutputSchema(
        resolution=Resolution.REBUILD,
        data=RebuildData(
            package="sample",
            jira_issue=work.issue,
            dependency_issue="RHEL-2",
            dependency_component="golang",
            fix_version="rhel-9.8.z",
        ),
    )
    work.handoffs = [
        _task_handoff(work.request, "rebuild_queue_c9s", work.state.model_dump(mode="json"), "rebuild")
    ]

    async def discover(_key, _pending, _snapshot):
        return [JiraMember(work.issue, [WAITING], "New", ["rhel-9.8.z"], [], [])]

    @asynccontextmanager
    async def shared_gateway(*_args):
        yield []

    installed = False

    async def buildroot(state, tools):
        assert state == work.state and tools == []
        return installed, "golang-1.2.3-4.el9"

    flexmock(coordination).should_receive("_discover").replace_with(discover)
    flexmock(coordination).should_receive("_gateway").replace_with(shared_gateway)
    flexmock(coordination).should_receive("read_rebuild_buildroot").replace_with(buildroot)
    blocked = await plan_package_stream(work.key, [work], [])
    assert not blocked.ready and "target buildroot" in blocked.blockers[work.issue]
    installed = True
    ready = await plan_package_stream(work.key, [work], blocked.members)
    assert ready.ready[work.issue].handoffs == work.handoffs


@pytest.mark.asyncio
async def test_recovery_resumes_waiting_intent_and_preserves_manual_override():
    accepted = []

    async def jira(path, params=None):
        if path == "myself":
            return {"accountId": "bot"}
        labels = [WAITING]
        if path.endswith("RHEL-2"):
            labels.append(JiraLabels.TRIAGED_BACKPORT.value)
        return {"fields": {"labels": labels, "status": {"name": "New"}, "updated": "1"}}

    async def findings(issue, authors, kind):
        assert issue == "RHEL-1" and authors == {"bot"} and kind == "triage-finding"
        return [{"coordination_workflow_id": "package-stream/fixture", "user_triggered": True}]

    async def submit(_client, intent, *, task_queue):
        accepted.append((intent, task_queue))
        return "accepted"

    async def describe():
        return flexmock(status=WorkflowExecutionStatus.COMPLETED)

    async def waiting(_jql):
        return [JiraMember(key, [WAITING], "New", [], [], []) for key in ("RHEL-1", "RHEL-2")]

    flexmock(coordination).should_receive("_search_members").replace_with(waiting)
    client = flexmock(get_workflow_handle=lambda _key: flexmock(describe=describe))
    flexmock(coordination).should_receive("jira_api_get").replace_with(jira)
    flexmock(coordination).should_receive("_jira_records").replace_with(findings)
    flexmock(coordination).should_receive("submit_triage_intent").replace_with(submit)
    result = await coordination.recover_package_coordination(client, "control")
    assert result["RHEL-1"] == "accepted"
    assert "Jira and GitLab" in result["RHEL-2"]
    [(intent, queue)] = accepted
    assert queue == "control"
    assert intent.user_triggered and intent.retry_label == WAITING and not intent.dry_run


@pytest.mark.asyncio
async def test_publication_records_the_handoff_for_the_coordinator_without_dispatch():
    work = make_work("RHEL-1")
    work.request.analysis.follow_up_requested = True
    result = TriageAnalysisResult(work.state, follow_up_requested=True)
    written = []

    async def child(*_args, **_kwargs):
        return result

    async def execute(fn, *args, **_kwargs):
        written.append((fn.__name__, args))
        if fn == publication.read_publication_inputs:
            return PublicationInputs([], "New", False)
        return None

    flexmock(publication.workflow).should_receive("info").and_return(flexmock(workflow_id="triage/RHEL-1"))
    flexmock(publication.workflow).should_receive("execute_child_workflow").replace_with(child)
    flexmock(publication).should_receive("_run_activity").replace_with(execute)

    result = await publication._run_request(work.request, "fixture", task_queue="fixture-pod")
    assert result.destinations == []
    assert [handoff.purpose for handoff in result.coordinated_handoffs] == ["backport"]
    assert [name for name, _ in written] == [
        "read_publication_inputs",
        "publish_triage_finding",
        "apply_triage_labels",
    ]
    assert written[1][1][1].coordination_workflow_id == work.key.workflow_id
    assert WAITING in written[-1][1][1]


@pytest.mark.asyncio
async def test_package_discovery_uses_current_labels_for_the_submitting_issue():
    work = make_work("RHEL-1")

    async def latest(path, params):
        assert path == "issue/RHEL-1" and "labels" in params["fields"]
        return {
            "fields": {"labels": [WAITING], "status": {"name": "New"}, "fixVersions": [{"name": "rhel-9.8"}]}
        }

    async def search(jql):
        assert 'component = "sample"' in jql
        return [JiraMember(work.issue, [], "New", ["rhel-9.8"], [], [])]

    flexmock(coordination).should_receive("jira_api_get").replace_with(latest)
    flexmock(coordination).should_receive("_search_members").replace_with(search)
    [member] = await coordination._discover(work.key, [work], [])
    assert member.labels == [WAITING]


@pytest.mark.asyncio
async def test_release_rechecks_the_higher_mr_before_writing():
    work = make_work("RHEL-1")

    async def jira(path, params=None):
        if path == "myself":
            return {"accountId": "bot"}
        return {"fields": {"labels": [WAITING], "status": {"name": "New"}}}

    async def findings(issue, authors, kind):
        return [{"request_id": work.request.request_id}]

    async def outdated(source):
        return False

    flexmock(coordination).should_receive("jira_api_get").replace_with(jira)
    flexmock(coordination).should_receive("_jira_records").replace_with(findings)
    flexmock(coordination).should_receive("_current_mr").replace_with(outdated).once()
    flexmock(coordination).should_receive("enqueue_triage_handoff").never()
    flexmock(coordination).should_receive("apply_triage_labels").never()
    source = ValidatedFix("RHEL-2", MR, "a" * 40, "c10s", "backport", [PATCH])
    assert not await coordination.release_coordinated_triage(work, PlannedRelease(work.handoffs, [source]))
