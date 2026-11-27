"""Package-stream ownership and dependency gates for Temporal triage."""

import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError, FailureError
from temporalio.service import RPCError, RPCStatusCode

from ymir.agents.temporal_triage_analysis import _gateway
from ymir.agents.temporal_triage_publication import (
    PackageStream,
    RedisHandoff,
    TriagePublicationRequest,
    _run_activity,
    _task_handoff,
    apply_triage_labels,
    enqueue_triage_handoff,
    stream_rank,
)
from ymir.agents.triage_agent import TriageState, read_rebuild_buildroot
from ymir.common.base_utils import redis_client
from ymir.common.constants import JiraLabels
from ymir.common.jira_api import jira_api_get, jira_api_post
from ymir.common.models import ConsolidatedIssue, RebaseData, RebuildData, Resolution, Task
from ymir.common.temporal_triage import TriageIntent, submit_triage_intent
from ymir.common.version_utils import get_fix_version_variants

WAIT_INTERVAL = timedelta(minutes=15)
WAITING = JiraLabels.TRIAGE_COORDINATION_WAITING.value
_JIRA_FIELDS = ["labels", "status", "fixVersions", "updated"]
_JIRA_RECORD = re.compile(r"\{code:json\}\s*(\{.*?\})\s*\{code\}", re.DOTALL)
_TRIAGED = frozenset(
    {
        WAITING,
        JiraLabels.TRIAGED_NOT_AFFECTED.value,
        JiraLabels.TRIAGED.value,
        JiraLabels.TRIAGED_BACKPORT.value,
        JiraLabels.TRIAGED_REBASE.value,
        JiraLabels.TRIAGED_REBUILD.value,
        JiraLabels.REBASED.value,
        JiraLabels.BACKPORTED.value,
        JiraLabels.REBUILT.value,
        JiraLabels.MERGED.value,
        JiraLabels.TRIAGE_ERRORED.value,
        JiraLabels.MANUAL_BRANCH_NEEDED.value,
        JiraLabels.NEEDS_ATTENTION.value,
        *JiraLabels.deferred_triage_labels(),
        *JiraLabels.postponement_labels(),
    }
)


@dataclass
class CoordinatedTriage:
    key: PackageStream
    request: TriagePublicationRequest
    state: TriageState
    handoffs: list[RedisHandoff]

    @property
    def issue(self) -> str:
        return self.request.analysis.jira_issue


def _jira_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass
class JiraMember:
    issue: str
    labels: list[str]
    status: str
    versions: list[str]
    updated: str = "initial"

    @classmethod
    def from_fields(cls, issue: str, fields: dict):
        labels = fields["labels"]
        return cls(
            issue,
            labels,
            fields["status"]["name"],
            [version["name"] for version in fields["fixVersions"]],
            fields.get("updated") or "initial",
        )

    @property
    def triaged(self) -> bool:
        return bool(_TRIAGED.intersection(self.labels)) or self.status in {"Closed", "Done"}


async def _search_members(jql: str) -> list[JiraMember]:
    members = {}
    token = None
    for _ in range(10):
        query = {"jql": jql, "fields": _JIRA_FIELDS, "maxResults": 100}
        if token:
            query["nextPageToken"] = token
        page = await jira_api_post("search/jql", json=query, api_version="3", decode_response=True)
        for item in page["issues"]:
            members[item["key"]] = JiraMember.from_fields(item["key"], item["fields"])
        if page.get("isLast") is True:
            return list(members.values())
        next_token = page.get("nextPageToken")
        if not next_token or next_token == token:
            break
        token = next_token
    raise ApplicationError("Incomplete Jira package-stream discovery", type="IncompleteTriageDiscovery")


async def _discover(
    key: PackageStream, work: list[CoordinatedTriage], snapshot: list[str]
) -> list[JiraMember]:
    versions = ", ".join(
        _jira_literal(item)
        for item in dict.fromkeys([*get_fix_version_variants(key.version), key.version + ".0.z"])
    )
    package = _jira_literal(key.package)
    component = _jira_literal(key.component)
    package_selector = (
        f"cf[10669] = {component}"
        if "/" in key.component
        else f"(component = {package} OR cf[10669] = {component})"
    )

    fields = {"fields": ",".join(_JIRA_FIELDS)}
    current = {}
    for item in work:
        issue = await jira_api_get(f"issue/{item.issue}", params=fields)
        current[item.issue] = JiraMember.from_fields(item.issue, issue["fields"])

    own_keys = ", ".join(map(_jira_literal, sorted(set(current) | set(snapshot))))
    matches = await _search_members(
        f"project = RHEL AND (key in ({own_keys}) OR ({package_selector} "
        f'AND fixVersion in ({versions}) AND status not in ("Closed", "Done")))'
    )
    # Jira's search index can lag behind the waiting label we just wrote.
    return list(({member.issue: member for member in matches} | current).values())


async def _triage_findings(issue: str, account: str) -> list[dict]:
    start = 0
    records = []
    for _ in range(30):
        page = await jira_api_get(f"issue/{issue}/comment", params={"startAt": start, "maxResults": 100})
        comments = page["comments"]
        if page["startAt"] != start:
            raise ValueError(f"Unexpected Jira comment page for {issue}")
        for comment in comments:
            if comment.get("author", {}).get("accountId") != account:
                continue
            if not isinstance(body := comment.get("body"), str):
                continue
            for match in _JIRA_RECORD.finditer(body):
                try:
                    record = json.loads(match.group(1))
                except ValueError:
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("schema") == 1
                    and record.get("kind") == "triage-finding"
                ):
                    records.append(record)
        start += len(comments)
        if start >= page["total"]:
            return list(reversed(records))
        if not comments:
            break
    raise ValueError(f"Incomplete Jira comments for {issue}")


@dataclass
class PlannedRelease:
    handoffs: list[RedisHandoff]
    siblings: list[CoordinatedTriage] = field(default_factory=list)


def _dependency_cycle(item: CoordinatedTriage, pending: list[CoordinatedTriage]) -> bool:
    dependencies = {
        work.issue: data.dependency_issue
        for work in pending
        if isinstance(data := work.state.triage_result.data, RebuildData)
    }
    seen = set()
    issue = item.issue
    while issue in dependencies:
        if issue in seen:
            return True
        seen.add(issue)
        issue = dependencies[issue]
    return False


@dataclass
class CoordinationPlan:
    members: list[str]
    blockers: dict[str, str]
    ready: dict[str, PlannedRelease]
    stale: list[str]
    missing_triage: dict[str, str] = field(default_factory=dict)


def combine_compatible_handoffs(pending: list[CoordinatedTriage], ready: dict[str, PlannedRelease]) -> None:
    groups = {}
    for item in sorted(pending, key=lambda work: work.issue):
        if item.issue not in ready:
            continue
        data = item.state.triage_result.data
        if isinstance(data, RebaseData):
            kind = ("rebase", data.version)
        elif isinstance(data, RebuildData) and data.dependency_issue and data.dependency_component:
            kind = ("rebuild", data.dependency_issue, data.dependency_component, data.side_tag)
        else:
            continue
        groups.setdefault((kind, item.request.analysis.user_triggered), []).append(item)

    for group in groups.values():
        if len(group) < 2:
            continue
        primary, *siblings = group
        decision = ready[primary.issue]
        resolution = primary.state.triage_result.resolution.value
        for index, handoff in enumerate(decision.handoffs):
            if handoff.purpose != resolution:
                continue
            state = TriageState.model_validate(Task.model_validate_json(handoff.payload).metadata)
            finding = state.triage_result
            for sibling in siblings:
                data = sibling.state.triage_result.data
                finding.data.consolidated_issues.append(
                    ConsolidatedIssue(
                        issue_key=sibling.issue,
                        cve_id=data.cve_id,
                        dependency_issue=getattr(data, "dependency_issue", None),
                        dependency_component=getattr(data, "dependency_component", None),
                    )
                )
            decision.handoffs[index] = _task_handoff(
                primary.request, handoff.destination, state.model_dump(mode="json"), handoff.purpose
            )
        decision.siblings = siblings
        for sibling in siblings:
            sibling_plan = ready.pop(sibling.issue)
            decision.handoffs.extend(
                handoff for handoff in sibling_plan.handoffs if handoff.purpose != resolution
            )


@activity.defn
async def plan_package_stream(
    key: PackageStream, pending: list[CoordinatedTriage], snapshot: list[str]
) -> CoordinationPlan:
    members = await _discover(key, pending, snapshot)
    by_key = {member.issue: member for member in members}
    target = stream_rank(key.version)
    if not snapshot:
        snapshot = sorted(
            member.issue
            for member in members
            if any(stream_rank(version) == target for version in member.versions)
        )
    pending_keys = {item.issue for item in pending}
    missing = [issue for issue in snapshot if issue not in by_key]
    unregistered = [
        issue
        for issue in snapshot
        if issue in by_key
        and WAITING in by_key[issue].labels
        and issue not in pending_keys
        and by_key[issue].status not in {"Closed", "Done"}
    ]
    untriaged = [issue for issue in snapshot if issue in by_key and not by_key[issue].triaged]
    ready, blockers, stale = {}, {}, []
    for item in pending:
        current = by_key.get(item.issue)
        if not current:
            blockers[item.issue] = "Jira discovery did not return this issue."
            continue
        if (
            WAITING not in current.labels
            or current.status in {"Closed", "Done"}
            or not any(stream_rank(version) == target for version in current.versions)
        ):
            stale.append(item.issue)
            continue
        if JiraLabels.TRIAGE_IN_PROGRESS.value in current.labels:
            blockers[item.issue] = "A newer triage request is in progress."
            continue
        if missing or untriaged or unregistered:
            blockers[item.issue] = "Waiting for sibling triage: " + ", ".join(
                missing + untriaged + unregistered
            )
            continue
        data = item.state.triage_result.data
        if isinstance(data, RebuildData) and (data.dependency_issue or data.dependency_component):
            if _dependency_cycle(item, pending):
                blockers[item.issue] = "The rebuild dependency graph contains a cycle."
                continue
            async with _gateway(item.issue, item.request.analysis.config_override) as tools:
                dependency = await read_rebuild_buildroot(item.state, tools)
            if dependency is None or not dependency[0]:
                blockers[item.issue] = "Waiting for the fixed dependency in the target buildroot."
                continue
        ready[item.issue] = PlannedRelease(item.handoffs.copy())
    missing_triage = {
        issue: by_key[issue].updated
        for issue in untriaged
        if not any(label.startswith("ymir_") for label in by_key[issue].labels)
    }
    combine_compatible_handoffs(pending, ready)
    return CoordinationPlan(snapshot, blockers, ready, stale, missing_triage)


@activity.defn
async def release_coordinated_triage(item: CoordinatedTriage, release: PlannedRelease) -> bool:
    account = (await jira_api_get("myself"))["accountId"]
    current = []
    for work in [item, *release.siblings]:
        fields = (await jira_api_get(f"issue/{work.issue}", params={"fields": "labels,status"}))["fields"]
        labels = fields["labels"]
        if WAITING not in labels or fields["status"]["name"] in {"Closed", "Done"}:
            continue
        if JiraLabels.TRIAGE_IN_PROGRESS.value in labels:
            return False
        findings = await _triage_findings(work.issue, account)
        if not findings or findings[0].get("request_id") != work.request.request_id:
            return bool(findings) and not release.siblings
        current.append(work)
    if not current:
        return True
    if len(current) != 1 + len(release.siblings):
        # If the batch read changed before dispatch, replan from Jira. Once all
        # handoffs were accepted, the receipt lets a retry finish removing labels.
        async with redis_client(os.environ["REDIS_URL"]) as redis:
            for handoff in release.handoffs:
                if not await redis.exists(handoff.receipt_key):
                    return False
        for work in current:
            await apply_triage_labels(work.request, [], [WAITING])
        return True

    label = {
        Resolution.BACKPORT: JiraLabels.TRIAGED_BACKPORT,
        Resolution.REBASE: JiraLabels.TRIAGED_REBASE,
        Resolution.REBUILD: JiraLabels.TRIAGED_REBUILD,
    }[item.state.triage_result.resolution]
    for work in current:
        await apply_triage_labels(work.request, [label.value], [])
    for handoff in release.handoffs:
        await enqueue_triage_handoff(handoff)
    for work in current:
        await apply_triage_labels(work.request, [], [WAITING])
    return True


@dataclass
class PackageStreamRun:
    key: PackageStream
    pending: list[CoordinatedTriage] = field(default_factory=list)
    snapshot: list[str] = field(default_factory=list)


@workflow.defn
class PackageStreamWorkflow:
    @workflow.init
    def __init__(self, state: PackageStreamRun):
        self.pending = {item.issue: item for item in state.pending}
        self.snapshot = state.snapshot
        self.revision = 0
        self.blockers: dict[str, str] = {}

    @workflow.signal
    def reconcile(self, item: CoordinatedTriage) -> None:
        self.pending[item.issue] = item
        self.revision += 1

    @workflow.query
    def status(self) -> dict:
        return {"members": self.snapshot, "pending": sorted(self.pending), "blockers": self.blockers}

    @workflow.run
    async def run(self, state: PackageStreamRun) -> None:
        if workflow.info().workflow_id != state.key.workflow_id:
            raise ApplicationError("Expected the canonical package-stream workflow ID", non_retryable=True)
        while self.pending:
            revision = self.revision
            planning = list(self.pending.values())
            try:
                plan = await _run_activity(plan_package_stream, state.key, planning, self.snapshot)
                self.snapshot = plan.members
                self.blockers = plan.blockers
                if plan.missing_triage:
                    await _run_activity(PackageStreamActivities.submit_sibling_triage, plan.missing_triage)
                for item in planning:
                    if self.pending.get(item.issue) != item:
                        continue
                    if item.issue in plan.ready:
                        release = plan.ready[item.issue]
                        if any(self.pending.get(sibling.issue) != sibling for sibling in release.siblings):
                            continue
                        if not await _run_activity(release_coordinated_triage, item, release):
                            self.blockers[item.issue] = "Waiting to recheck changed Jira inputs."
                            continue
                        completed = [item, *release.siblings]
                    elif item.issue in plan.stale:
                        completed = [item]
                    else:
                        continue
                    for work in completed:
                        if self.pending.get(work.issue) == work:
                            self.pending.pop(work.issue)
            except FailureError:
                self.blockers = dict.fromkeys(
                    self.pending, "Jira discovery or handoff temporarily unavailable"
                )
            if not self.pending:
                return
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(
                    PackageStreamRun(state.key, list(self.pending.values()), self.snapshot)
                )
            with suppress(TimeoutError):
                await workflow.wait_condition(
                    lambda revision=revision: self.revision != revision, timeout=WAIT_INTERVAL
                )


class PackageStreamActivities:
    def __init__(self, client, queue: str):
        self.client = client
        self.queue = queue

    @activity.defn
    async def submit_sibling_triage(self, issues: dict[str, str]) -> None:
        for issue, updated in issues.items():
            await submit_triage_intent(
                self.client,
                TriageIntent(issue, f"discovery:{updated}", "discovery", dry_run=False),
                task_queue=self.queue,
            )

    @activity.defn
    async def submit_package_coordination(self, item: CoordinatedTriage) -> None:
        await self.client.start_workflow(
            PackageStreamWorkflow.run,
            PackageStreamRun(item.key),
            id=item.key.workflow_id,
            task_queue=self.queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="reconcile",
            start_signal_args=[item],
            rpc_timeout=timedelta(seconds=20),
        )


async def recover_package_coordination(client, queue: str) -> dict[str, str]:
    """Recover waiting issue intent from Jira in a fresh Temporal namespace."""
    account = (await jira_api_get("myself"))["accountId"]
    recovered = {}
    waiting = await _search_members(
        f'project = RHEL AND labels = "{WAITING}" AND status not in ("Closed", "Done")'
    )
    for member in sorted(waiting, key=lambda record: record.issue):
        issue = member.issue
        fields = (await jira_api_get(f"issue/{issue}", params={"fields": "labels,status,updated"}))["fields"]
        labels = fields["labels"]
        if WAITING not in labels or fields["status"]["name"] in {"Closed", "Done"}:
            continue
        if {
            JiraLabels.TRIAGED_BACKPORT.value,
            JiraLabels.TRIAGED_REBASE.value,
            JiraLabels.TRIAGED_REBUILD.value,
            JiraLabels.BACKPORTED.value,
            JiraLabels.REBASED.value,
            JiraLabels.REBUILT.value,
            JiraLabels.MERGED.value,
        }.intersection(labels) or any(label.endswith("_in_progress") for label in labels):
            recovered[issue] = "Check Jira and GitLab for a downstream handoff before resubmitting"
            continue
        findings = await _triage_findings(issue, account)
        if not findings or not (workflow_id := findings[0].get("coordination_workflow_id")):
            recovered[issue] = "Waiting issue has no package-stream finding"
            continue
        try:
            status = await client.get_workflow_handle(workflow_id).describe()
            if status.status == WorkflowExecutionStatus.RUNNING:
                owner = client.get_workflow_handle(workflow_id)
                if issue in (await owner.query("status"))["pending"]:
                    continue
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
        recovered[issue] = await submit_triage_intent(
            client,
            TriageIntent(
                issue,
                f"coordination-recovery:{fields['updated']}",
                "user" if findings[0].get("user_triggered") is True else "retry",
                dry_run=False,
                force_cve_triage=findings[0].get("force_cve_triage") is True,
                retry_label=WAITING,
            ),
            task_queue=queue,
        )
    return recovered
