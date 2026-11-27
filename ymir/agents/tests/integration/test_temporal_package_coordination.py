"""Verify package-stream gating against local Temporal and mocked Jira."""

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
from ymir.agents import temporal_triage_ingress as ingress
from ymir.agents import temporal_triage_publication as publication
from ymir.agents.temporal_package_coordination import (
    WAITING,
    CoordinatedTriage,
    JiraMember,
    PackageStreamActivities,
    PackageStreamWorkflow,
    PlannedRelease,
    plan_package_stream,
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
from ymir.common.models import (
    BackportData,
    ConfidenceAssessment,
    RebaseData,
    RebuildData,
    Resolution,
    Task,
    TriageOutputSchema,
)

ADDRESS = os.getenv("TEMPORAL_TEST_ADDRESS")


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


def make_rebuild_work(issue: str, *, package="sample") -> CoordinatedTriage:
    work = make_work(issue, package=package)
    work.state.triage_result = TriageOutputSchema(
        resolution=Resolution.REBUILD,
        data=RebuildData(
            package=package,
            jira_issue=work.issue,
            dependency_issue="RHEL-2",
            dependency_component="golang",
            fix_version="rhel-9.8.z",
        ),
    )
    work.handoffs = [
        _task_handoff(work.request, "rebuild_queue_c9s", work.state.model_dump(mode="json"), "rebuild")
    ]
    return work


@pytest.mark.asyncio
@pytest.mark.skipif(not ADDRESS, reason="Set TEMPORAL_TEST_ADDRESS to a local Temporal server")
async def test_package_workflow_waits_without_pod_and_releases_independent_work():
    token = uuid4().hex
    package = f"sample-{token}"
    issue = make_rebuild_work("RHEL-1", package=package)
    independent = make_work("RHEL-3", package=package)
    member = JiraMember(issue.issue, [WAITING], "New", ["rhel-9.8.z"])
    neighbor = JiraMember(independent.issue, [], "New", ["rhel-9.8.z"])
    other_stream = JiraMember("RHEL-4", [], "New", ["rhel-10.2"])
    installed = False
    released = []
    capacity = asyncio.Event()

    async def discover(_key, _pending, _snapshot):
        return [member, neighbor, other_stream]

    @asynccontextmanager
    async def shared_gateway(*_args):
        yield []

    async def buildroot(state, tools):
        assert state == issue.state and tools == []
        return installed, "golang-1.2.3-4.el9"

    flexmock(coordination).should_receive("_discover").replace_with(discover)
    flexmock(coordination).should_receive("_gateway").replace_with(shared_gateway)
    flexmock(coordination).should_receive("read_rebuild_buildroot").replace_with(buildroot)

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
        status = await handle.query(PackageStreamWorkflow.status)
        assert "target buildroot" in status["blockers"][issue.issue]
        assert released[0][1].handoffs == independent.handoffs
        installed = True
        await submit.submit_package_coordination(issue)
        await asyncio.wait_for(handle.result(), 15)
        assert [key for key, _ in released] == [independent.issue, issue.issue]
        assert released[-1][1].handoffs == issue.handoffs

    history = await handle.fetch_history()
    activities = {
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    }
    assert activities == {"plan_package_stream", "submit_sibling_triage", "release_coordinated_triage"}


@pytest.mark.parametrize("resolution", [Resolution.REBASE, Resolution.REBUILD])
def test_compatible_fixes_use_one_legacy_handoff(resolution):
    if resolution == Resolution.REBASE:
        primary, sibling = make_work("RHEL-1"), make_work("RHEL-2")
        for item in (primary, sibling):
            item.state.triage_result = TriageOutputSchema(
                resolution=Resolution.REBASE,
                data=RebaseData(
                    package="sample", version="2.0", jira_issue=item.issue, fix_version="rhel-9.8.z"
                ),
            )
            item.handoffs = [
                _task_handoff(item.request, "rebase_queue_c9s", item.state.model_dump(mode="json"), "rebase")
            ]
    else:
        primary, sibling = make_rebuild_work("RHEL-1"), make_rebuild_work("RHEL-3")
    ready = {item.issue: PlannedRelease(item.handoffs.copy()) for item in (primary, sibling)}
    coordination.combine_compatible_handoffs([sibling, primary], ready)
    assert list(ready) == [primary.issue]
    decision = ready[primary.issue]
    assert [item.issue for item in decision.siblings] == [sibling.issue]
    [handoff] = decision.handoffs
    assert handoff.purpose == resolution.value
    task = Task.model_validate_json(handoff.payload)
    data = TriageState.model_validate(task.metadata).triage_result.data
    assert data.all_jira_issues == [primary.issue, sibling.issue]


@pytest.mark.asyncio
async def test_rebuild_dependency_is_checked_only_in_shared_planner():
    work = make_rebuild_work("RHEL-1")

    async def discover(_key, _pending, _snapshot):
        return [JiraMember(work.issue, [WAITING], "New", ["rhel-9.8.z"])]

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
@pytest.mark.parametrize(("user_triggered", "triggered_by"), [(True, "user"), (False, "retry")])
async def test_recovery_resumes_waiting_intent_and_preserves_manual_override(user_triggered, triggered_by):
    accepted = []

    async def jira(path, params=None):
        if path == "myself":
            return {"accountId": "bot"}
        labels = [WAITING]
        if path.endswith("RHEL-2"):
            labels.append(JiraLabels.TRIAGED_BACKPORT.value)
        return {"fields": {"labels": labels, "status": {"name": "New"}, "updated": "1"}}

    async def findings(issue, account):
        assert issue == "RHEL-1" and account == "bot"
        return [{"coordination_workflow_id": "package-stream/fixture", "user_triggered": user_triggered}]

    async def submit(_client, intent, *, task_queue):
        accepted.append((intent, task_queue))
        return "accepted"

    async def describe():
        return flexmock(status=WorkflowExecutionStatus.COMPLETED)

    async def waiting(_jql):
        return [JiraMember(key, [WAITING], "New", []) for key in ("RHEL-1", "RHEL-2")]

    flexmock(coordination).should_receive("_search_members").replace_with(waiting)
    client = flexmock(get_workflow_handle=lambda _key: flexmock(describe=describe))
    flexmock(coordination).should_receive("jira_api_get").replace_with(jira)
    flexmock(coordination).should_receive("_triage_findings").replace_with(findings)
    flexmock(coordination).should_receive("submit_triage_intent").replace_with(submit)
    result = await coordination.recover_package_coordination(client, "control")
    assert result["RHEL-1"] == "accepted"
    assert "Jira and GitLab" in result["RHEL-2"]
    [(intent, queue)] = accepted
    assert queue == "control"
    assert intent.triggered_by == triggered_by and intent.retry_label == WAITING and not intent.dry_run

    request = TriagePublicationRequest(
        TriageAnalysisRequest(intent.issue, {}, 1), intent.request_id, dry_run=intent.dry_run
    )

    async def apply_labels(publication_request, add, remove):
        assert publication_request is request
        assert add == [JiraLabels.TRIAGE_IN_PROGRESS.value]
        assert remove == [WAITING, JiraLabels.RETRY_NEEDED.value]

    flexmock(ingress).should_receive("apply_triage_labels").replace_with(apply_labels).once()
    await ingress.begin_triage_intent(request, intent, skipped=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", [Resolution.BACKPORT, Resolution.REBASE])
async def test_publication_records_the_handoff_for_the_coordinator_without_dispatch(resolution):
    work = make_work("RHEL-1")
    labels = []
    if resolution == Resolution.REBASE:
        work.state.triage_result = TriageOutputSchema(
            resolution=resolution,
            data=RebaseData(package="sample", version="2.0", jira_issue=work.issue, fix_version="rhel-9.8.z"),
        )
        labels = ["ymir_rebase_sibling", "ymir_rebase_waiting_for_siblings"]
    work.request.analysis.follow_up_requested = True
    result = TriageAnalysisResult(work.state, follow_up_requested=True)
    written = []

    async def child(*_args, **_kwargs):
        return result

    async def execute(fn, *args, **_kwargs):
        written.append((fn.__name__, args))
        if fn == publication.read_publication_inputs:
            return PublicationInputs(labels, "New", False)
        return None

    flexmock(publication.workflow).should_receive("info").and_return(flexmock(workflow_id="triage/RHEL-1"))
    flexmock(publication.workflow).should_receive("execute_child_workflow").replace_with(child)
    flexmock(publication).should_receive("_run_activity").replace_with(execute)

    result = await publication._run_request(work.request, "fixture", task_queue="fixture-pod")
    assert result.destinations == []
    assert [handoff.purpose for handoff in result.coordinated_handoffs] == [resolution.value]
    assert [name for name, _ in written] == [
        "read_publication_inputs",
        "publish_triage_finding",
        "apply_triage_labels",
    ]
    assert written[1][1][1].coordination_workflow_id == work.key.workflow_id
    assert WAITING in written[-1][1][1]


@pytest.mark.parametrize("level", [None, "low", "medium", "high"])
def test_publication_replaces_previous_confidence_label(level):
    work = make_work("RHEL-1")
    if level:
        work.state.triage_result.confidence = ConfidenceAssessment(level=level, reason="Source evidence")
    confidence_labels = {
        JiraLabels.APPLICABILITY_CONFIDENCE_LOW.value,
        JiraLabels.APPLICABILITY_CONFIDENCE_MEDIUM.value,
        JiraLabels.APPLICABILITY_CONFIDENCE_HIGH.value,
    }
    expected = {f"ymir_applicability_confidence_{level}"} if level else set()

    add, remove = publication._terminal_labels(TriageAnalysisResult(work.state, True))

    assert set(add) & confidence_labels == expected
    assert set(remove) & confidence_labels == confidence_labels - expected


@pytest.mark.asyncio
@pytest.mark.parametrize("branch_exists", [False, True])
async def test_publication_reports_manual_branch_hold(branch_exists):
    work = make_work("RHEL-1")
    work.state.automatic_branch_creation_disabled = True
    work.state.target_branch_exists = branch_exists
    result = TriageAnalysisResult(work.state, follow_up_requested=True)
    waiting = not branch_exists

    async def post_comment(request, step, body):
        assert request is work.request
        assert step == "finding"
        assert ("Automatic branch creation is disabled" in body) is waiting
        assert ("create the branch manually" in body) is waiting
        assert ("`ymir_todo` label to RHEL-1" in body) is waiting

    flexmock(publication).should_receive("_post_comment").replace_with(post_comment).once()
    handoff = publication._downstream_handoff(work.request, result)
    assert (handoff is None) is waiting
    await publication.publish_triage_finding(work.request, result, [handoff] if handoff else [])
    add, _ = publication._terminal_labels(result)
    assert (JiraLabels.MANUAL_BRANCH_NEEDED.value in add) is waiting


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
        assert "AND fixVersion in (" in jql
        assert 'AND status not in ("Closed", "Done")' in jql
        return [JiraMember(work.issue, [], "New", ["rhel-9.8"])]

    flexmock(coordination).should_receive("jira_api_get").replace_with(latest)
    flexmock(coordination).should_receive("_search_members").replace_with(search)
    [member] = await coordination._discover(work.key, [work], [])
    assert member.labels == [WAITING]
