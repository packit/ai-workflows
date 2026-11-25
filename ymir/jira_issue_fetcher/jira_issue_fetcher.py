#!/usr/bin/env python3
"""
Jira Issue Fetcher Script

This script fetches issues from Jira using a custom JQL query (QUERY)
and submits eligible triage requests to Temporal.

Follows Jira API best practices:
https://spaces.redhat.com/spaces/JiraAid/pages/553618479/Optimizing+scripts+that+make+API+calls

- Pagination for large datasets
- Rate limiting (5 calls per second)
- Exponential backoff for retries
- Proper error handling and logging
- Optimized API calls with field filtering
- Timeouts
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import backoff
import redis.asyncio as redis
import requests

from ymir.common.base_utils import fix_await, get_jira_auth_headers, redis_client
from ymir.common.constants import JIRA_SEARCH_PATH, JiraLabels, RedisQueues
from ymir.common.logging_setup import configure_logging
from ymir.common.merge_queue import SubmitResult, submit_merge_job
from ymir.common.models import (
    BackportOutputSchema,
    ErrorData,
    ErrorListEntry,
    OpenEndedAnalysisData,
    RebaseOutputSchema,
    Task,
)
from ymir.common.temporal_triage import (
    TriageIntent,
    connect_triage_ingress,
    submit_triage_intent,
    temporal_triage_accepts_requests,
)
from ymir.common.version_utils import construct_internal_branch_name, parse_rhel_version

logger = logging.getLogger(__name__)

# Name of the Jira group that membership in identifies a Red Hat Employee.
# Matches the value used by ymir/tools/privileged/jira.py.
_RH_EMPLOYEE_GROUP = "Red Hat Employee"


class JiraIssueFetcher:
    DEFAULT_QUERY = "project=RHEL and assignee = jotnar-project"
    MAX_RESULTS_PER_PAGE = 500  # Optimize for fewer, more expensive calls
    RATE_LIMIT_CALLS_PER_SECOND = 5
    RATE_LIMIT_DELAY = 1.0 / RATE_LIMIT_CALLS_PER_SECOND  # 0.2 seconds between calls
    API_TIMEOUT = 90  # 90 seconds timeout
    MODULAR_COMPONENT_PATTERN = re.compile(r".+:.+/.+")

    # "In-flight" labels: each marks an issue as currently being worked on by
    # an agent. If an agent crashes (SIGKILL/OOM) after setting one of these
    # but before reaching the label write that replaces it with an outcome,
    # the label is stuck forever and the fetcher skips the issue on every
    # subsequent sweep — this is the bug the staleness check below guards
    # against. (Planned redeployments are instead handled instantly by the
    # SIGTERM handler in run_task_loop; this is the safety net for everything
    # else.)
    IN_FLIGHT_LABELS = (
        JiraLabels.TRIAGE_IN_PROGRESS.value,
        JiraLabels.TRIAGED_BACKPORT.value,
        JiraLabels.TRIAGED_REBASE.value,
        JiraLabels.TRIAGED_REBUILD.value,
        JiraLabels.CLI_TRIAGE.value,
        JiraLabels.REPRODUCER_IN_PROGRESS.value,
    )

    def __init__(self):
        self.jira_url = os.environ["JIRA_URL"]
        self.redis_url = os.environ["REDIS_URL"]

        # Allow query override from environment. Component exclusions live in the
        # Jira filter the QUERY points at (maintained in the cve-scope repo:
        # https://gitlab.cee.redhat.com/jotnar-project/cve-scope),
        # not here. ymir_todo bypasses the filter and processes any component.
        self.query = os.getenv("QUERY", self.DEFAULT_QUERY)

        # Optional: maximum number of issues to fetch
        max_issues_str = os.getenv("MAX_ISSUES", "")
        self.max_issues: int | None = int(max_issues_str) if max_issues_str else None

        # Use constant page size
        self.max_results_per_page = self.MAX_RESULTS_PER_PAGE

        # Staleness threshold for the in-flight-label safety net (see
        # IN_FLIGHT_LABELS above). Conservative default pending real task
        # duration data from Phoenix traces (http://localhost:6006/) — the
        # longest documented single phase today is the 3h post-push testing
        # window (POST_PUSH_TESTING_TIMEOUT), and a full task can involve
        # several such phases plus retries, so 24h leaves comfortable margin
        # while still being far short of "stuck forever". Too aggressive a
        # value risks a false-positive re-enqueue of a still-running task —
        # two agents processing the same issue concurrently.
        self.stale_label_threshold_hours = float(os.getenv("STALE_LABEL_THRESHOLD_HOURS", "24"))

        self.headers = get_jira_auth_headers()

        # Rate limiting
        self.last_request_time = 0.0

        self.dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

        # Rollout flag: code defaults to "true" (safe for local runs without the
        # ConfigMap); the cluster ConfigMap sets "false" to enable modular triage.
        self.skip_modular = os.getenv("SKIP_MODULAR", "true").lower() == "true"
        logger.info(
            "SKIP_MODULAR=%s — modular issues will be %s",
            self.skip_modular,
            "skipped" if self.skip_modular else "enqueued for triage",
        )

    async def _rate_limit(self):
        """Enforce rate limiting of RATE_LIMIT_CALLS_PER_SECOND calls per second"""
        current_time = time.time()
        time_since_last_request = current_time - self.last_request_time

        if time_since_last_request < self.RATE_LIMIT_DELAY:
            sleep_time = self.RATE_LIMIT_DELAY - time_since_last_request
            logger.debug(f"Rate limiting: sleeping for {sleep_time:.3f} seconds")
            await asyncio.sleep(sleep_time)

        self.last_request_time = time.time()

    @backoff.on_exception(
        backoff.expo,
        (requests.RequestException, requests.HTTPError),
        max_tries=4,  # 1 initial + 3 retries
        base=2,
        logger=logger,
    )
    def _make_request_with_retries(self, url: str, json_data: dict[str, Any]) -> dict[str, Any]:
        """
        Make HTTP request with exponential backoff retries
        """
        response = requests.post(url, json=json_data, headers=self.headers, timeout=self.API_TIMEOUT)

        # Handle rate limiting specifically
        if response.status_code == 429:
            logger.warning("Rate limited (429), will retry with backoff")
            raise requests.HTTPError("Rate limited", response=response)

        response.raise_for_status()
        return response.json()

    @backoff.on_exception(
        backoff.expo,
        (requests.RequestException, requests.HTTPError),
        max_tries=4,
        base=2,
        logger=logger,
    )
    def _edit_jira_labels(self, issue_key: str, add: list[str], remove: list[str]) -> None:
        """
        Atomically add/remove labels on a Jira issue via PUT /rest/api/3/issue/{key}.

        Raises on permanent failure after retries. Callers must skip side effects
        (e.g. Redis enqueue) when this raises — otherwise the next sweep would
        re-pick-up the same issue without the in-progress marker.
        """
        url = urljoin(self.jira_url, f"rest/api/3/issue/{issue_key}")
        update_ops: list[dict[str, str]] = [{"add": label} for label in add]
        update_ops.extend({"remove": label} for label in remove)
        payload = {"update": {"labels": update_ops}}

        response = requests.put(url, json=payload, headers=self.headers, timeout=self.API_TIMEOUT)
        if response.status_code == 429:
            logger.warning(f"Rate limited (429) editing labels on {issue_key}, will retry")
            raise requests.HTTPError("Rate limited", response=response)
        response.raise_for_status()

    @backoff.on_exception(
        backoff.expo,
        (requests.RequestException, requests.HTTPError),
        max_tries=4,
        base=2,
        logger=logger,
    )
    def _post_jira_comment(self, issue_key: str, comment: str) -> None:
        """Post a private comment to a Jira issue."""
        url = urljoin(self.jira_url, f"rest/api/3/issue/{issue_key}/comment")
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
            },
            "visibility": {"type": "group", "value": "Red Hat Employee"},
        }
        response = requests.post(url, json=payload, headers=self.headers, timeout=self.API_TIMEOUT)
        if response.status_code == 429:
            logger.warning(f"Rate limited (429) posting comment on {issue_key}, will retry")
            raise requests.HTTPError("Rate limited", response=response)
        response.raise_for_status()

    @backoff.on_exception(
        backoff.expo,
        (requests.RequestException, requests.HTTPError),
        max_tries=4,
        base=2,
        logger=logger,
    )
    def _make_get_request_with_retries(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make a GET request with exponential backoff retries."""
        response = requests.get(url, params=params, headers=self.headers, timeout=self.API_TIMEOUT)
        if response.status_code == 429:
            logger.warning("Rate limited (429), will retry with backoff")
            raise requests.HTTPError("Rate limited", response=response)
        response.raise_for_status()
        return response.json()

    def _label_added_by_rh_employee(self, issue_key: str, label: str | None = None) -> bool:
        return self.authorized_trigger_event(issue_key, label) is not None

    def authorized_trigger_event(self, issue_key: str, label: str | None = None) -> str | None:
        """Verify that the latest add of a label was performed by a Red Hat Employee.

        Checks per-issue that the label was added by a Red Hat Employee rather
        than (e.g.) an external collaborator. Fetches the full changelog via
        the dedicated /rest/api/3/issue/{issueKey}/changelog endpoint with
        pagination to handle issues with long histories (which may exceed the
        100-entry limit when expanded inline). Picks the most-recent add event
        for the specified label and looks up that author's Jira group memberships.

        Args:
            issue_key: Jira issue key (e.g., RHEL-12345)
            label: Label to check. Defaults to ymir_todo for backwards compatibility.

        Returns no event on an author verification or parsing failure — that path skips the
        issue with a warning rather than treating an unverifiable label as a
        legitimate trigger.
        """
        if label is None:
            label = JiraLabels.TODO.value

        try:
            # Fetch full changelog with pagination to handle long histories
            changelog_url = urljoin(self.jira_url, f"rest/api/3/issue/{issue_key}/changelog")
            latest_add_author: str | None = None
            latest_add_time = ""
            latest_add_id = None
            start_at = 0
            max_results = 100  # Jira default and max per request

            while True:
                data = self._make_get_request_with_retries(
                    changelog_url,
                    params={"startAt": start_at, "maxResults": max_results},
                )
                histories = data.get("values", [])

                if not histories:
                    break

                # Find the most-recent entry that adds the specified label. Track by
                # `created` timestamp so the result is order-independent (ISO 8601
                # strings are lexically comparable).
                for history in histories:
                    created = history.get("created") or ""
                    for item in history.get("items", []):
                        if item.get("field") != "labels":
                            continue
                        from_labels = set((item.get("fromString") or "").split())
                        to_labels = set((item.get("toString") or "").split())
                        if label in (to_labels - from_labels) and created > latest_add_time:
                            latest_add_time = created
                            latest_add_id = str(history.get("id") or created)
                            latest_add_author = (history.get("author") or {}).get("accountId")
                        break  # one labels item per history

                # Check if there are more pages
                is_last_page = data.get("isLastPage", True)
                if is_last_page:
                    break

                start_at += max_results

            if not latest_add_author:
                logger.warning(
                    f"No changelog entry adds {label} to {issue_key}; "
                    f"cannot verify author, treating as non-RH-employee"
                )
                return None

            user_data = self._make_get_request_with_retries(
                urljoin(self.jira_url, "rest/api/3/user"),
                params={"accountId": latest_add_author, "expand": "groups"},
            )
            groups = user_data.get("groups") or {}
            items = groups.get("items") or []
            group_names = [g.get("name") for g in items if g]
            return latest_add_id if _RH_EMPLOYEE_GROUP in group_names else None
        except requests.HTTPError as e:
            if e.response is not None:
                status_code = e.response.status_code
                # 401/403: auth/permission errors - re-raise as transient to avoid deleting valid labels
                if status_code in (401, 403):
                    logger.warning(
                        f"Auth/permission error verifying {label} author on {issue_key}: {e}; "
                        f"will retry next sweep (not removing label)"
                    )
                    raise
                # 400/404: bad request or not found - treat as verification failure
                if status_code in (400, 404):
                    logger.warning(
                        f"Permanent API error verifying {label} author on {issue_key}: {e}; "
                        f"treating as non-RH-employee"
                    )
                    return None
            raise
        except (ValueError, KeyError, AttributeError) as e:
            logger.warning(f"Failed to parse {label} author on {issue_key}: {e}; treating as non-RH-employee")
            return None

    @staticmethod
    def _is_label_stale(issue: dict[str, Any], threshold_hours: float) -> bool:
        """Check whether `issue`'s bulk-fetched `fields.updated` timestamp is
        older than `threshold_hours`.

        Uses data already in hand from the bulk search — no extra per-issue
        Jira API calls, so this scales to the full sweep with zero added
        load. This is intentionally coarser than "when was this specific
        label added": any field update (e.g. an unrelated human comment)
        resets `updated` too. Acceptable for a best-effort safety net —
        false negatives (staleness masked by an unrelated update) just wait
        for the next sweep, which is safe.

        Returns False (not stale) if `updated` is missing or unparseable,
        since we can't tell without it — fails closed to the current
        "always skip" behavior rather than risking a false-positive
        re-enqueue of still-running work.
        """
        updated_str = (issue.get("fields") or {}).get("updated")
        if not updated_str:
            return False
        try:
            updated = datetime.fromisoformat(updated_str)
        except ValueError:
            return False
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return datetime.now(UTC) - updated > timedelta(hours=threshold_hours)

    def _find_stale_in_flight_label(self, issue: dict[str, Any], ymir_labels: list[str]) -> str | None:
        """Return the in-flight label on `issue` if it looks abandoned, else None.

        "Abandoned" means: one of IN_FLIGHT_LABELS is present, no blocking
        coexisting Ymir label, and the issue hasn't been updated in over
        ``stale_label_threshold_hours``.

        For most in-flight labels, any other Ymir label blocks recovery (the
        same signal triage_agent's dedup check uses — outcome written but
        in-flight not cleaned up, or an orthogonal workflow label such as
        ``ymir_consolidate_base``). Applicability-confidence labels are
        assessment metadata, not workflow state, so they never block recovery
        even though they coexist with both triage and downstream stages.

        ``ymir_reproducer_in_progress`` is special: reproducer runs in parallel
        with triage/fix agents and deliberately leaves their outcome labels
        in place. Only another ``ymir_reproducer_*`` label means that stage
        already finished (or was partially cleaned up).
        """
        ignorable = {
            JiraLabels.RETRY_NEEDED.value,
            JiraLabels.TODO.value,
            JiraLabels.TODO_TRIAGE.value,
            JiraLabels.TRIAGE_ONLY.value,
            JiraLabels.APPLICABILITY_CONFIDENCE_LOW.value,
            JiraLabels.APPLICABILITY_CONFIDENCE_MEDIUM.value,
            JiraLabels.APPLICABILITY_CONFIDENCE_HIGH.value,
        }
        reproducer_in_progress = JiraLabels.REPRODUCER_IN_PROGRESS.value
        for label in self.IN_FLIGHT_LABELS:
            if label not in ymir_labels:
                continue
            if label == reproducer_in_progress:
                blocking = [ol for ol in ymir_labels if ol != label and ol.startswith("ymir_reproducer_")]
            else:
                blocking = [ol for ol in ymir_labels if ol != label and ol not in ignorable]
            if blocking:
                continue
            if self._is_label_stale(issue, self.stale_label_threshold_hours):
                return label
        return None

    async def search_issues(self) -> list[dict[str, Any]]:
        """
        Search for issues using the configured query with cursor-based pagination.
        The /rest/api/3/search/jql endpoint uses nextPageToken instead of startAt.
        """
        logger.info(f"Starting issue search with query: {self.query}")

        all_issues = []
        next_page_token = None

        fields = [
            "key",  # Issue key (e.g., RHEL-12345)
            "labels",  # Issue labels
            "components",  # Issue components
            "customfield_10669",  # Downstream Component Name
            "fixVersions",  # Fix Version/s (e.g., rhel-9.8)
            "updated",  # Last-modified timestamp — used for stale in-flight-label detection
        ]

        while True:
            await self._rate_limit()

            json_payload = {
                "jql": self.query,
                "maxResults": self.max_results_per_page,
                "fields": fields,
            }

            if next_page_token:
                json_payload["nextPageToken"] = next_page_token

            logger.info(
                f"Fetching issues: maxResults={self.max_results_per_page}, nextPageToken={next_page_token}"
            )

            try:
                url = urljoin(self.jira_url, JIRA_SEARCH_PATH)
                response_data = self._make_request_with_retries(url, json_data=json_payload)

                issues = response_data.get("issues", [])
                all_issues.extend(issues)

                total_issues = response_data.get("total", len(all_issues))
                logger.info(
                    f"Retrieved {len(issues)} issues (total so far: {len(all_issues)}/{total_issues})"
                )

                next_page_token = response_data.get("nextPageToken")
                if not next_page_token or len(issues) == 0:
                    break

            except Exception as e:
                logger.error(f"Error fetching issues: {e}")
                raise

        # It seems that Jira issue keys are not case-sensitive, convert them
        # all to upper-case here so that we can use them in sets and direct comparisons
        for issue in all_issues:
            issue["key"] = issue["key"].upper()

        logger.info(f"Successfully retrieved {len(all_issues)} issues")
        return all_issues

    async def _get_existing_issue_keys(self, redis_conn: redis.Redis) -> set[str]:
        """
        Get all existing issue keys from all Redis queues to avoid duplicates
        """
        try:
            # All Redis queues and lists to check
            queue_names = list(RedisQueues.all_queues())

            existing_keys = set()

            for queue_name in queue_names:
                try:
                    # Get all items from the current queue
                    queue_items = await fix_await(redis_conn.lrange(queue_name, 0, -1))
                    queue_count = 0

                    for item in queue_items:
                        try:
                            issue_key = None

                            # For input queues, parse as Task and extract from metadata
                            if queue_name in RedisQueues.input_queues():
                                task = Task.model_validate_json(item)
                                if task.metadata:
                                    match queue_name:
                                        case (
                                            RedisQueues.REBASE_QUEUE_C9S.value
                                            | RedisQueues.REBASE_QUEUE_C10S.value
                                            | RedisQueues.BACKPORT_QUEUE_C9S.value
                                            | RedisQueues.BACKPORT_QUEUE_C10S.value
                                            | RedisQueues.REBUILD_QUEUE_C9S.value
                                            | RedisQueues.REBUILD_QUEUE_C10S.value
                                            | RedisQueues.REBASE_QUEUE_C9S_TODO.value
                                            | RedisQueues.REBASE_QUEUE_C10S_TODO.value
                                            | RedisQueues.BACKPORT_QUEUE_C9S_TODO.value
                                            | RedisQueues.BACKPORT_QUEUE_C10S_TODO.value
                                            | RedisQueues.REBUILD_QUEUE_C9S_TODO.value
                                            | RedisQueues.REBUILD_QUEUE_C10S_TODO.value
                                            | RedisQueues.CLARIFICATION_NEEDED_QUEUE.value
                                            | RedisQueues.BACKPORT_QUEUE.value
                                            | RedisQueues.REBASE_QUEUE.value
                                            | RedisQueues.REPRODUCER_QUEUE.value
                                            | RedisQueues.REPRODUCER_QUEUE_TODO.value
                                        ):
                                            issue_key = task.metadata.get("jira_issue", "").upper()
                                        case _:
                                            continue

                            # For result/data queues, parse the data directly
                            else:
                                try:
                                    match queue_name:
                                        case RedisQueues.COMPLETED_REBASE_LIST.value:
                                            schema = RebaseOutputSchema.model_validate_json(item)
                                            # Schema doesn't have issue keys, skip these
                                            continue
                                        case RedisQueues.COMPLETED_BACKPORT_LIST.value:
                                            schema = BackportOutputSchema.model_validate_json(item)
                                            # Schema doesn't have issue keys, skip these
                                            continue
                                        case RedisQueues.OPEN_ENDED_ANALYSIS_LIST.value:
                                            schema = OpenEndedAnalysisData.model_validate_json(item)
                                            issue_key = schema.jira_issue.upper()
                                        case RedisQueues.ERROR_LIST.value:
                                            try:
                                                entry = ErrorListEntry.model_validate_json(item)
                                                issue_key = entry.error.jira_issue.upper()
                                            except ValueError:
                                                # Legacy entry pushed before ErrorListEntry existed.
                                                schema = ErrorData.model_validate_json(item)
                                                issue_key = schema.jira_issue.upper()
                                        case _:
                                            continue
                                except ValueError:
                                    # Fallback to task parsing for these queues if direct parsing fails
                                    task = Task.model_validate_json(item)
                                    if task.metadata and "issue" in task.metadata:
                                        issue_key = task.metadata["issue"].upper()

                            if issue_key:
                                existing_keys.add(issue_key)
                                queue_count += 1

                        except (json.JSONDecodeError, ValueError) as e:
                            logger.warning(f"Failed to parse item from {queue_name}: {e}")
                            continue

                    if queue_count > 0:
                        logger.info(f"Found {queue_count} existing issues in {queue_name}")

                except Exception as e:
                    logger.warning(f"Error checking {queue_name}: {e}")
                    continue

            logger.info(f"Found {len(existing_keys)} total existing issues across all queues")
            return existing_keys

        except Exception as e:
            logger.error(f"Error checking existing queue items: {e}")
            return set()

    async def submit_temporal_issues(self, issues: list[dict[str, Any]]) -> int:
        if not issues or not temporal_triage_accepts_requests():
            return 0

        client = None
        queued_keys = None
        admitted = 0
        submitted = set()
        for issue in issues:
            if not (key := issue.get("key")) or key in submitted:
                continue
            if self.max_issues is not None and admitted >= self.max_issues:
                break
            try:
                intent = self._temporal_intent(issue)
                if intent is None:
                    continue
                if intent.source == "discovery" or (
                    intent.retry_label and intent.retry_label != JiraLabels.RETRY_NEEDED.value
                ):
                    if queued_keys is None:
                        async with redis_client(self.redis_url) as redis_conn:
                            queued_keys = await self._get_existing_issue_keys(redis_conn)
                    if intent.issue in queued_keys:
                        logger.info("Issue %s already has downstream work", intent.issue)
                        continue
                if client is None:
                    client = await connect_triage_ingress()
                status = await submit_triage_intent(client, intent)
                logger.info("Temporal triage %s for %s (%s)", status, intent.issue, intent.request_id)
                submitted.add(key)
                admitted += 1
            except Exception:
                logger.exception("Could not submit Temporal triage for %s", issue["key"])
        return admitted

    def _temporal_intent(self, issue: dict[str, Any]) -> TriageIntent | None:
        key = issue["key"]
        fields = issue.get("fields") or {}
        labels = [label for label in fields.get("labels", []) if label.startswith("ymir_")]
        triggers = {}
        unauthorized = []
        for label in (JiraLabels.TODO.value, JiraLabels.TODO_TRIAGE.value):
            if label in labels:
                if event := self.authorized_trigger_event(key, label):
                    triggers[label] = event
                else:
                    unauthorized.append(label)
        if unauthorized and not self.dry_run:
            self._edit_jira_labels(key, add=[], remove=unauthorized)
        if triggers:
            if any(
                label.endswith("_in_progress") and label != JiraLabels.TRIAGE_IN_PROGRESS.value
                for label in labels
            ):
                return None
            primary = next(iter(triggers))
            return TriageIntent(
                key,
                f"jira:{primary}:{triggers[primary]}",
                "jira",
                dry_run=self.dry_run,
                user_triggered=True,
                triage_only=JiraLabels.TODO.value not in triggers,
                triggers=triggers,
            )

        if {JiraLabels.TODO.value, JiraLabels.TODO_TRIAGE.value}.intersection(labels):
            return None

        if self.skip_modular and self.MODULAR_COMPONENT_PATTERN.match(fields.get("customfield_10669") or ""):
            return None
        retry = JiraLabels.RETRY_NEEDED.value
        stale = self._find_stale_in_flight_label(issue, labels)
        if stale or (retry in labels and not any(label.endswith("_in_progress") for label in labels)):
            return TriageIntent(
                key,
                f"retry:{fields.get('updated', 'initial')}",
                "retry",
                dry_run=self.dry_run,
                triage_only=JiraLabels.TRIAGE_ONLY.value in labels,
                retry_label=stale or retry,
            )
        if not labels:
            return TriageIntent(
                key, f"discovery:{fields.get('updated', 'initial')}", "discovery", dry_run=self.dry_run
            )
        return None

    @staticmethod
    def _resolve_branch_from_fix_versions(fix_versions: list[dict]) -> str | None:
        """Derive dist-git branch name from Jira fixVersions.

        Returns the internal branch (e.g. ``rhel-9.8.0``) or None.
        """
        for fv in fix_versions:
            name = fv.get("name", "")
            parsed = parse_rhel_version(name)
            if parsed:
                major, minor, _is_zstream = parsed
                return construct_internal_branch_name(major, minor)
        return None

    async def _process_consolidation_labels(
        self,
        issues: list[dict[str, Any]],
    ) -> int:
        """Scan for ymir_consolidate_base / _next label pairs and submit targeted jobs.

        Verifies that both consolidation labels were added by Red Hat Employees
        to prevent untrusted external collaborators from submitting consolidation
        jobs. Removes labels that fail verification.

        Returns the number of consolidation jobs submitted.
        """
        base_bucket: dict[str, dict] = {}
        next_bucket: dict[str, dict] = {}

        for issue in issues:
            issue_key = issue.get("key")
            if not issue_key:
                continue
            fields = issue.get("fields", {})
            labels = fields.get("labels", [])

            is_base = JiraLabels.CONSOLIDATE_BASE.value in labels
            is_next = JiraLabels.CONSOLIDATE_NEXT.value in labels
            if not is_base and not is_next:
                continue

            # Verify that consolidation labels were added by Red Hat Employees
            # to prevent untrusted external collaborators from triggering jobs
            labels_to_remove = []
            if is_base:
                try:
                    is_rh_employee = self._label_added_by_rh_employee(
                        issue_key, JiraLabels.CONSOLIDATE_BASE.value
                    )
                except requests.RequestException as e:
                    logger.warning(
                        f"Transient error verifying {JiraLabels.CONSOLIDATE_BASE.value} author on "
                        f"{issue_key}: {e}; skipping for this sweep"
                    )
                    continue

                if not is_rh_employee:
                    logger.warning(
                        f"Issue {issue_key} has {JiraLabels.CONSOLIDATE_BASE.value} but the label "
                        f"was not added by a Red Hat Employee - skipping and removing the label"
                    )
                    labels_to_remove.append(JiraLabels.CONSOLIDATE_BASE.value)
                    is_base = False

            if is_next:
                try:
                    is_rh_employee = self._label_added_by_rh_employee(
                        issue_key, JiraLabels.CONSOLIDATE_NEXT.value
                    )
                except requests.RequestException as e:
                    logger.warning(
                        f"Transient error verifying {JiraLabels.CONSOLIDATE_NEXT.value} author on "
                        f"{issue_key}: {e}; skipping for this sweep"
                    )
                    continue

                if not is_rh_employee:
                    logger.warning(
                        f"Issue {issue_key} has {JiraLabels.CONSOLIDATE_NEXT.value} but the label "
                        f"was not added by a Red Hat Employee - skipping and removing the label"
                    )
                    labels_to_remove.append(JiraLabels.CONSOLIDATE_NEXT.value)
                    is_next = False

            # Remove any labels that failed verification
            if labels_to_remove:
                if self.dry_run:
                    logger.info(f"DRY_RUN: would remove {labels_to_remove} from {issue_key}")
                else:
                    try:
                        self._edit_jira_labels(issue_key, add=[], remove=labels_to_remove)
                    except Exception as e:
                        logger.warning(f"Failed to remove {labels_to_remove} from {issue_key}: {e}")
                continue

            # MR consolidation doesn't support modular packages yet (the consolidation
            # build never enables a module stream in the Copr buildroot) — skip rather
            # than risk a broken consolidated build. Labels are left untouched so a
            # maintainer can see why nothing happened.
            downstream_component = fields.get("customfield_10669") or ""
            if self.MODULAR_COMPONENT_PATTERN.match(downstream_component):
                logger.warning(
                    "Issue %s has a consolidation label but is a modular issue (%s) — "
                    "skipping, MR consolidation for modular packages is not yet supported",
                    issue_key,
                    downstream_component,
                )
                continue

            components = [c.get("name") for c in (fields.get("components") or []) if c.get("name")]
            if not components:
                logger.warning(
                    "Issue %s has consolidation label but no component, skipping",
                    issue_key,
                )
                continue
            component = components[0]

            fix_versions = fields.get("fixVersions", [])
            branch = self._resolve_branch_from_fix_versions(fix_versions)
            if not branch:
                logger.warning(
                    "Issue %s has consolidation label but no resolvable fixVersion (%s), skipping",
                    issue_key,
                    fix_versions,
                )
                continue

            bucket_key = f"{component}:{branch}"
            entry = {"key": issue_key, "component": component, "branch": branch}
            if is_base:
                base_bucket[bucket_key] = entry
            if is_next:
                next_bucket[bucket_key] = entry

        submitted = 0
        async with redis_client(self.redis_url) as redis_conn:
            for bucket_key, base_entry in base_bucket.items():
                next_entry = next_bucket.pop(bucket_key, None)
                if not next_entry:
                    logger.warning(
                        "Issue %s has %s but no matching %s for %s",
                        base_entry["key"],
                        JiraLabels.CONSOLIDATE_BASE.value,
                        JiraLabels.CONSOLIDATE_NEXT.value,
                        bucket_key,
                    )
                    continue

                package = base_entry["component"]
                branch = base_entry["branch"]
                base_key = base_entry["key"]
                next_key = next_entry["key"]

                try:
                    result = await submit_merge_job(
                        redis_conn,
                        package,
                        branch,
                        source_issues=[base_key, next_key],
                    )
                except Exception as e:
                    logger.error(
                        "Failed to submit consolidation job for %s/%s (%s, %s): %s",
                        package,
                        branch,
                        base_key,
                        next_key,
                        e,
                    )
                    continue

                if result is not SubmitResult.SUBMITTED:
                    logger.info(
                        "Consolidation job already queued for %s/%s, removing labels",
                        package,
                        branch,
                    )

                # Remove labels from both issues
                for issue_key, label in [
                    (base_key, JiraLabels.CONSOLIDATE_BASE.value),
                    (next_key, JiraLabels.CONSOLIDATE_NEXT.value),
                ]:
                    if self.dry_run:
                        logger.info("DRY_RUN: would remove %s from %s", label, issue_key)
                    else:
                        try:
                            self._edit_jira_labels(issue_key, add=[], remove=[label])
                        except Exception as e:
                            logger.warning("Failed to remove %s from %s: %s", label, issue_key, e)

                if result is SubmitResult.SUBMITTED:
                    comment = (
                        f"MR consolidation job submitted for {package}/{branch}. "
                        f"The backport MRs for {base_key} and {next_key} will be "
                        f"consolidated into a single MR."
                    )
                elif result is SubmitResult.CONFLICT:
                    comment = (
                        f"A consolidation job is already pending or running for "
                        f"{package}/{branch}. Consolidation labels have been "
                        f"removed from {base_key} and {next_key}. Please "
                        f"re-apply them after the current job finishes."
                    )
                else:
                    comment = None

                if comment is not None:
                    for issue_key in [base_key, next_key]:
                        if self.dry_run:
                            logger.info("DRY_RUN: would post comment on %s", issue_key)
                        else:
                            try:
                                self._post_jira_comment(issue_key, comment)
                            except Exception as e:
                                logger.warning("Failed to post comment on %s: %s", issue_key, e)

                if result is SubmitResult.SUBMITTED:
                    submitted += 1
                    logger.info(
                        "Submitted consolidation job for %s/%s (issues: %s, %s)",
                        package,
                        branch,
                        base_key,
                        next_key,
                    )

        for bucket_key, next_entry in next_bucket.items():
            logger.warning(
                "Issue %s has %s but no matching %s for %s",
                next_entry["key"],
                JiraLabels.CONSOLIDATE_NEXT.value,
                JiraLabels.CONSOLIDATE_BASE.value,
                bucket_key,
            )

        return submitted

    async def run(self) -> None:
        try:
            logger.info("Starting Jira issue fetcher")

            issues = await self.search_issues()

            if not issues:
                logger.info("No issues found matching the query")
                return

            submitted = await self.submit_temporal_issues(issues)
            logger.info("Completed: %d triage requests submitted to Temporal", submitted)

            consolidation_count = await self._process_consolidation_labels(issues)
            if consolidation_count:
                logger.info(f"Submitted {consolidation_count} MR consolidation job(s)")

        except Exception as e:
            logger.error(f"Fatal error in issue fetcher: {e}")
            raise


async def main():
    try:
        fetcher = JiraIssueFetcher()
        await fetcher.run()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Application failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    configure_logging(level=logging.INFO)
    required_vars = ["JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "REDIS_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.info("Required environment variables:")
        logger.info("  JIRA_URL - Jira instance URL (e.g., https://redhat.atlassian.net)")
        logger.info("  JIRA_EMAIL - Jira account email for authentication")
        logger.info("  JIRA_TOKEN - Jira API token")
        logger.info("  REDIS_URL - Redis connection URL (e.g., redis://localhost:6379)")
        sys.exit(1)

    if os.getenv("QUERY"):
        logger.info("Using QUERY from environment variable")

    asyncio.run(main())
