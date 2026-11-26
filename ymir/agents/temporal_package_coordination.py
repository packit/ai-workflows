"""Package-stream ownership and dependency gates for Temporal triage."""

import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from urllib.parse import quote, urlsplit

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
    _jira_record,
    _post_comment,
    _run_activity,
    _task_handoff,
    apply_triage_labels,
    enqueue_triage_handoff,
    stream_rank,
)
from ymir.agents.triage_agent import TriageState, read_rebuild_buildroot
from ymir.common.base_utils import redis_client
from ymir.common.constants import JiraLabels
from ymir.common.cve import extract_cve_ids
from ymir.common.gitlab_api import gitlab_api_get
from ymir.common.jira_api import jira_api_get, jira_api_post
from ymir.common.models import BackportData, ConsolidatedIssue, RebaseData, RebuildData, Resolution, Task
from ymir.common.temporal_triage import TriageIntent, submit_triage_intent
from ymir.common.version_utils import get_fix_version_variants

WAIT_INTERVAL = timedelta(minutes=15)
WAITING = JiraLabels.TRIAGE_COORDINATION_WAITING.value
_JIRA_FIELDS = ["labels", "summary", "status", "fixVersions", "issuelinks", "updated"]
_VALIDATION_RECORD = re.compile(r"\{code:json\}\s*(\{.*?\})\s*\{code\}", re.DOTALL)
_MR_PATH = re.compile(r"/(redhat/(?:rhel|centos-stream)/rpms/[^/]+)/-/merge_requests/(\d+)")
_PINNED_PATCH = re.compile(r"/(?:commit|commits|raw)/[0-9a-f]{40}(?:[/.]|$)")
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


def _clone_keys(fields: dict) -> set[str]:
    return {
        other["key"]
        for link in fields.get("issuelinks") or []
        if (link.get("type") or {}).get("name", "").casefold() == "cloners"
        and (other := link.get("inwardIssue") or link.get("outwardIssue"))
    }


def _cves(value: str | None) -> set[str]:
    return set((extract_cve_ids(value) or "").replace(",", " ").upper().split())


@dataclass
class JiraMember:
    issue: str
    labels: list[str]
    status: str
    versions: list[str]
    cves: list[str]
    clones: list[str]
    updated: str = "initial"

    @classmethod
    def from_fields(cls, issue: str, fields: dict):
        labels = fields["labels"]
        return cls(
            issue,
            labels,
            fields["status"]["name"],
            [version["name"] for version in fields["fixVersions"]],
            sorted(_cves(" ".join([fields.get("summary") or "", *labels]))),
            sorted(_clone_keys(fields)),
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
    families = set()
    clone_keys = set()
    current = {}
    for item in work:
        fields = (await jira_api_get(f"issue/{item.issue}", params={"fields": ",".join(_JIRA_FIELDS)}))[
            "fields"
        ]
        member = JiraMember.from_fields(item.issue, fields)
        current[item.issue] = member
        families.update(member.cves)
        clone_keys.update(member.clones)
    selectors = [f'(fixVersion in ({versions}) AND status not in ("Closed", "Done"))']
    if families:
        selectors.append("labels in (" + ", ".join(map(_jira_literal, sorted(families))) + ")")
        selectors.extend(
            f"summary ~ {_jira_literal(chr(34) + family + chr(34))}" for family in sorted(families)
        )
    if clone_keys:
        selectors.append("key in (" + ", ".join(map(_jira_literal, sorted(clone_keys))) + ")")
    own_keys = ", ".join(map(_jira_literal, sorted({item.issue for item in work} | set(snapshot))))
    matches = await _search_members(
        f"project = RHEL AND (key in ({own_keys}) OR ({package_selector} AND ({' OR '.join(selectors)})))"
    )
    # Jira's search index can lag behind the waiting label we just wrote.
    return list(({member.issue: member for member in matches} | current).values())


def _configured_order() -> dict[tuple[int, int], int]:
    versions = os.getenv("TEMPORAL_TRIAGE_STREAM_ORDER", "").replace(",", " ").split()
    ranks = [stream_rank(version) for version in reversed(versions)]
    if None in ranks or len(set(ranks)) != len(ranks):
        raise ValueError("Stream order must contain distinct RHEL versions")
    return {rank: offset for offset, rank in enumerate(ranks)}


def predecessors(
    current: JiraMember, members: list[JiraMember], version: str, order: dict | None = None
) -> dict[str, list[JiraMember]]:
    """Select all ties on the nearest applicable higher stream for each issue family."""
    target = stream_rank(version)
    if target is None or (order and target not in order):
        raise ValueError(f"{version} is absent from the stream order")
    target_priority = order[target] if order else target
    families = current.cves or ["clone"]
    result = {}
    for family in families:
        ranked = []
        for member in members:
            if member.issue == current.issue or JiraLabels.TRIAGED_NOT_AFFECTED.value in member.labels:
                continue
            related = (
                family in member.cves
                if family != "clone"
                else member.issue in current.clones or current.issue in member.clones
            )
            if not related:
                continue
            ranks = [rank for value in member.versions if (rank := stream_rank(value))]
            if not ranks:
                raise ValueError(f"{member.issue} has no recognizable RHEL target")
            if order and any(rank not in order for rank in ranks):
                raise ValueError(f"{member.issue} has a version absent from the stream order")
            priorities = [order[rank] for rank in ranks] if order else ranks
            if higher := [priority for priority in priorities if priority > target_priority]:
                ranked.append((min(higher), member))
        if ranked:
            closest = min(rank for rank, _ in ranked)
            result[family] = [member for rank, member in ranked if rank == closest]
    return result


@dataclass
class ValidatedFix:
    issue: str
    merge_request: str
    sha: str
    target_branch: str
    resolution: str
    patch_urls: list[str] = field(default_factory=list)
    version: str | None = None
    dependency_component: str | None = None


async def _jira_records(issue: str, authors: set[str], kind: str) -> list[dict]:
    start = 0
    records = []
    for _ in range(30):
        page = await jira_api_get(f"issue/{issue}/comment", params={"startAt": start, "maxResults": 100})
        comments = page["comments"]
        if page["startAt"] != start:
            raise ValueError(f"Unexpected Jira comment page for {issue}")
        for comment in comments:
            if comment.get("author", {}).get("accountId") not in authors:
                continue
            if not isinstance(body := comment.get("body"), str):
                continue
            for match in _VALIDATION_RECORD.finditer(body):
                try:
                    record = json.loads(match.group(1))
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("schema") == 1 and record.get("kind") == kind:
                    records.append(record)
        start += len(comments)
        if start >= page["total"]:
            return list(reversed(records))
        if not comments:
            break
    raise ValueError(f"Incomplete Jira comments for {issue}")


async def _verify_not_affected(members: list[JiraMember]) -> None:
    label = JiraLabels.TRIAGED_NOT_AFFECTED.value
    unaffected = [member for member in members if label in member.labels]
    if not unaffected:
        return
    account = (await jira_api_get("myself"))["accountId"]
    for member in unaffected:
        findings = await _jira_records(member.issue, {account}, "triage-finding")
        if (
            not findings
            or (findings[0].get("finding") or {}).get("resolution") != Resolution.NOT_AFFECTED.value
        ):
            member.labels = [value for value in member.labels if value != label]


async def _current_mr(source: ValidatedFix) -> bool:
    match = _MR_PATH.fullmatch(urlsplit(source.merge_request).path)
    current = await gitlab_api_get(f"projects/{quote(match[1], safe='')}/merge_requests/{match[2]}")
    return (
        current.get("state") in {"opened", "merged"}
        and (current.get("diff_refs") or {}).get("head_sha") == source.sha
        and current.get("target_branch") == source.target_branch
    )


async def _validated_fix(package: str, member: JiraMember, family: str) -> ValidatedFix | None:
    authors = set(os.getenv("TEMPORAL_TRIAGE_VALIDATION_AUTHORS", "").replace(",", " ").split())
    if not authors:
        return None
    for record in await _jira_records(member.issue, authors, "triage-fix-validation"):
        mr = record.get("merge_request", "")
        if not isinstance(mr, str):
            continue
        url = urlsplit(mr)
        match = _MR_PATH.fullmatch(url.path)
        if (
            url.scheme != "https"
            or url.netloc != "gitlab.com"
            or not match
            or match[1].rsplit("/", 1)[-1] != package
            or record.get("issue") != member.issue
            or record.get("stream") not in member.versions
            or record.get("target_branch") is None
            or (family != "clone" and family not in record.get("cves", []))
            or record.get("resolution") not in {"backport", "rebase", "rebuild"}
        ):
            continue
        sha = record.get("source_sha", "")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        checks = record.get("checks", {})
        if not isinstance(checks, dict) or not isinstance(record.get("reproducer_required"), bool):
            continue
        required = {"build", "test", "coverage"}
        if record["resolution"] == "rebuild":
            required.add("rebuild")
        if record.get("reproducer_required") is True:
            required.add("paired_reproducer")
        if not all(
            isinstance(checks.get(check), str) and checks[check].startswith("https://") for check in required
        ):
            continue
        patches = record.get("patch_urls", [])
        if not isinstance(patches, list) or any(
            not isinstance(url, str)
            or not url.startswith("https://")
            or not _PINNED_PATCH.search(urlsplit(url).path)
            for url in patches
        ):
            continue
        evidence = ValidatedFix(
            member.issue,
            mr,
            sha,
            record["target_branch"],
            record["resolution"],
            patches,
            record.get("version"),
            record.get("dependency_component"),
        )
        if await _current_mr(evidence):
            return evidence
    return None


def adapt_handoffs(item: CoordinatedTriage, sources: list[ValidatedFix]) -> list[RedisHandoff] | None:
    if not sources:
        return item.handoffs
    output = item.state.triage_result
    data = output.data
    if any(source.resolution != output.resolution.value for source in sources):
        return None
    note = "\n".join(
        f"Validated higher-stream fix for {source.issue}: {source.merge_request} at {source.sha}."
        for source in sources
    )
    fields = {"triage_summary": "\n\n".join(filter(None, [data.triage_summary, note]))}
    if isinstance(data, BackportData):
        if any(not source.patch_urls for source in sources):
            return None
        fields["patch_urls"] = list(dict.fromkeys(url for source in sources for url in source.patch_urls))
    elif isinstance(data, RebaseData):
        if any(source.version != data.version for source in sources):
            return None
    elif isinstance(data, RebuildData):
        if not data.dependency_component or any(
            source.dependency_component != data.dependency_component for source in sources
        ):
            return None
    else:
        return None
    state = item.state.model_copy(
        update={"triage_result": output.model_copy(update={"data": data.model_copy(update=fields)})}
    )
    metadata = state.model_dump(mode="json")
    if isinstance(data, BackportData):
        metadata["ystream_inheritance_disabled"] = True
    adapted = []
    for handoff in item.handoffs:
        if handoff.purpose == output.resolution.value:
            handoff = _task_handoff(item.request, handoff.destination, metadata, handoff.purpose)
        elif handoff.purpose == "reproducer" and isinstance(data, BackportData):
            reproducer = Task.model_validate_json(handoff.payload).metadata
            reproducer["patch_urls"] = fields["patch_urls"]
            handoff = _task_handoff(item.request, handoff.destination, reproducer, handoff.purpose)
        adapted.append(handoff)
    return adapted


@dataclass
class PlannedRelease:
    handoffs: list[RedisHandoff]
    sources: list[ValidatedFix]
    siblings: list[CoordinatedTriage] = field(default_factory=list)
    member_sources: dict[str, list[ValidatedFix]] = field(default_factory=dict)


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
        decision.member_sources[primary.issue] = decision.sources.copy()
        for sibling in siblings:
            sibling_plan = ready.pop(sibling.issue)
            decision.member_sources[sibling.issue] = sibling_plan.sources
            decision.sources.extend(fix for fix in sibling_plan.sources if fix not in decision.sources)
            decision.handoffs.extend(
                handoff for handoff in sibling_plan.handoffs if handoff.purpose != resolution
            )


@activity.defn
async def plan_package_stream(
    key: PackageStream, pending: list[CoordinatedTriage], snapshot: list[str]
) -> CoordinationPlan:
    members = await _discover(key, pending, snapshot)
    await _verify_not_affected(members)
    order = _configured_order()
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
        try:
            edges = predecessors(current, members, key.version, order)
        except ValueError as error:
            blockers[item.issue] = str(error)
            continue
        sources = []
        for family, upstream in edges.items():
            for member in upstream:
                evidence = await _validated_fix(key.package, member, family)
                if evidence is None:
                    blockers[item.issue] = f"Waiting for validated higher-stream fix for {member.issue}."
                elif evidence not in sources:
                    sources.append(evidence)
        if item.issue not in blockers:
            if (
                edges
                and current.cves
                and set(edges) != set(current.cves)
                and isinstance(item.state.triage_result.data, BackportData)
            ):
                blockers[item.issue] = "Only some of the issue's CVEs have a higher-stream fix."
                continue
            if (handoffs := adapt_handoffs(item, sources)) is None:
                blockers[item.issue] = "Higher-stream fix conflicts with the lower-stream approach."
            else:
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
                ready[item.issue] = PlannedRelease(handoffs, sources)
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
        findings = await _jira_records(work.issue, {account}, "triage-finding")
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

    for source in release.sources:
        if not await _current_mr(source):
            return False

    label = {
        Resolution.BACKPORT: JiraLabels.TRIAGED_BACKPORT,
        Resolution.REBASE: JiraLabels.TRIAGED_REBASE,
        Resolution.REBUILD: JiraLabels.TRIAGED_REBUILD,
    }[item.state.triage_result.resolution]
    for work in current:
        sources = release.member_sources.get(work.issue, release.sources)
        if sources:
            revision = sha256(json.dumps(sorted(fix.sha for fix in sources)).encode()).hexdigest()
            await _post_comment(
                work.request,
                f"higher-stream-fixes:{revision}",
                "Selected validated higher-stream fixes.\n\n"
                + _jira_record(
                    {
                        "kind": "higher-stream-fixes",
                        "request_id": work.request.request_id,
                        "fixes": [
                            {"issue": fix.issue, "merge_request": fix.merge_request, "source_sha": fix.sha}
                            for fix in sources
                        ],
                    }
                ),
            )
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
                            self.blockers[item.issue] = "Waiting to recheck changed Jira or GitLab inputs."
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
                    self.pending, "Jira/GitLab discovery or handoff temporarily unavailable"
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
        findings = await _jira_records(issue, {account}, "triage-finding")
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
                "retry",
                dry_run=False,
                user_triggered=findings[0].get("user_triggered") is True,
                force_cve_triage=findings[0].get("force_cve_triage") is True,
                retry_label=WAITING,
            ),
            task_queue=queue,
        )
    return recovered
