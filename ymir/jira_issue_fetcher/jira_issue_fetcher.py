#!/usr/bin/env python3
"""
Jira Issue Fetcher Script

This script fetches issues from Jira using a custom JQL query (QUERY)
and pushes each found issue to the Redis triage_queue for processing.

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
from typing import Any
from urllib.parse import urljoin

import backoff
import redis.asyncio as redis
import requests

from ymir.common.base_utils import fix_await, get_jira_auth_headers, redis_client
from ymir.common.constants import JIRA_SEARCH_PATH, JiraLabels, RedisQueues
from ymir.common.logging_setup import configure_logging
from ymir.common.models import (
    BackportOutputSchema,
    ErrorData,
    OpenEndedAnalysisData,
    RebaseOutputSchema,
    Task,
    TriageInputSchema,
)

configure_logging(level=logging.INFO)
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

    def __init__(self):
        self.jira_url = os.environ["JIRA_URL"]
        self.redis_url = os.environ["REDIS_URL"]

        # Allow query override from environment
        self.query = os.getenv("QUERY", self.DEFAULT_QUERY)

        # Optional: comma-separated list of components to ignore
        ignored = os.getenv("IGNORED_COMPONENTS", "")
        self.ignored_components: set[str] = {c.strip().lower() for c in ignored.split(",") if c.strip()}

        # Optional: maximum number of issues to fetch
        max_issues_str = os.getenv("MAX_ISSUES", "")
        self.max_issues: int | None = int(max_issues_str) if max_issues_str else None

        # Use constant page size
        self.max_results_per_page = self.MAX_RESULTS_PER_PAGE

        self.headers = get_jira_auth_headers()

        # Rate limiting
        self.last_request_time = 0.0

        # DRY_RUN: skip Jira writes (atomic label flips for ymir_todo /
        # ymir_retry_needed) but still push tasks to Redis. The agent (also
        # presumably in DRY_RUN) handles the rest of the dry-mode flow.
        self.dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
        if self.dry_run:
            logger.info(
                "DRY_RUN=true — Jira label writes (atomic flips) will be "
                "skipped; Redis pushes will proceed normally"
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

    def _label_added_by_rh_employee(self, issue_key: str) -> bool:
        """Verify that the latest add of ymir_todo was performed by a Red Hat Employee.

        The JQL no longer gates ymir_todo on the assignee, so the fetcher must
        check per-issue that the label was added by a Red Hat Employee rather
        than (e.g.) an external collaborator. Walks the issue's changelog,
        picks the most-recent ``ymir_todo`` add event, and looks up that
        author's Jira group memberships.

        Returns False on any lookup or parsing failure — that path skips the
        issue with a warning rather than treating an unverifiable label as a
        legitimate trigger.
        """
        try:
            issue_url = urljoin(self.jira_url, f"rest/api/3/issue/{issue_key}?expand=changelog")
            response = requests.get(issue_url, headers=self.headers, timeout=self.API_TIMEOUT)
            response.raise_for_status()
            histories = response.json().get("changelog", {}).get("histories", [])

            # Find the most-recent entry that adds ymir_todo to labels. Track by
            # `created` timestamp so the result is order-independent (ISO 8601
            # strings are lexically comparable).
            latest_add_author: str | None = None
            latest_add_time = ""
            for history in histories:
                created = history.get("created") or ""
                for item in history.get("items", []):
                    if item.get("field") != "labels":
                        continue
                    from_labels = set((item.get("fromString") or "").split())
                    to_labels = set((item.get("toString") or "").split())
                    if JiraLabels.TODO.value in (to_labels - from_labels) and created > latest_add_time:
                        latest_add_time = created
                        latest_add_author = (history.get("author") or {}).get("accountId")
                    break  # one labels item per history

            if not latest_add_author:
                logger.warning(
                    f"No changelog entry adds {JiraLabels.TODO.value} to {issue_key}; "
                    f"cannot verify author, treating as non-RH-employee"
                )
                return False

            user_url = urljoin(
                self.jira_url,
                f"rest/api/3/user?accountId={latest_add_author}&expand=groups",
            )
            user_response = requests.get(user_url, headers=self.headers, timeout=self.API_TIMEOUT)
            user_response.raise_for_status()
            groups = user_response.json().get("groups") or {}
            items = groups.get("items") or []
            group_names = [g.get("name") for g in items if g]
            return _RH_EMPLOYEE_GROUP in group_names
        except Exception as e:
            logger.warning(
                f"Failed to verify {JiraLabels.TODO.value} author on {issue_key}: {e}; "
                f"treating as non-RH-employee"
            )
            return False

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
                                        case RedisQueues.TRIAGE_QUEUE.value:
                                            schema = TriageInputSchema.model_validate(task.metadata)
                                            issue_key = schema.issue.upper()
                                        case (
                                            RedisQueues.REBASE_QUEUE_C9S.value
                                            | RedisQueues.REBASE_QUEUE_C10S.value
                                            | RedisQueues.BACKPORT_QUEUE_C9S.value
                                            | RedisQueues.BACKPORT_QUEUE_C10S.value
                                            | RedisQueues.CLARIFICATION_NEEDED_QUEUE.value
                                            | RedisQueues.BACKPORT_QUEUE.value
                                            | RedisQueues.REBASE_QUEUE.value
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

    async def push_issues_to_queue(self, issues: list[dict[str, Any]]) -> int:
        """
        Push each issue to the Redis triage_queue, but only if it doesn't already exist
        """
        if not issues:
            logger.info("No issues to push to queue")
            return 0

        async with redis_client(self.redis_url) as redis_conn:
            # Get existing issue keys to avoid duplicates
            existing_keys = await self._get_existing_issue_keys(redis_conn)

            remove_issues_for_retry = set()
            user_triggered_keys = set()
            retry_needed_keys = set()
            # Extend existing_keys with issues that have Ymir labels (except ymir_retry_needed
            # and ymir_todo, which both signal a re-run is wanted).
            for issue in issues:
                issue_key = issue.get("key")
                if not issue_key:
                    continue

                fields = issue.get("fields", {})
                labels = fields.get("labels", [])
                ymir_labels = [label for label in labels if label.startswith("ymir_")]
                has_in_progress = any(label.endswith("_in_progress") for label in ymir_labels)

                # ymir_todo is the maintainer-facing trigger. A run already in progress
                # must not be re-enqueued — let it finish and the maintainer can re-add
                # the label later if needed.
                if JiraLabels.TODO.value in ymir_labels and not has_in_progress:
                    # The JQL no longer restricts ymir_todo by assignee, so we verify
                    # per-issue that the label was added by a Red Hat Employee. If
                    # not (or if the author can't be verified), skip the issue — the
                    # label may have been added by an external collaborator.
                    if not self._label_added_by_rh_employee(issue_key):
                        logger.warning(
                            f"Issue {issue_key} has {JiraLabels.TODO.value} but the "
                            f"label was not added by a Red Hat Employee - skipping"
                        )
                        existing_keys.add(issue_key)
                        continue
                    logger.info(
                        f"Issue {issue_key} has {JiraLabels.TODO.value} added by an "
                        f"RH employee - marking for user-triggered run"
                    )
                    remove_issues_for_retry.add(issue_key)
                    user_triggered_keys.add(issue_key)
                    continue

                # If issue has Ymir labels and there is no ymir_retry_needed label, mark as existing
                if ymir_labels and JiraLabels.RETRY_NEEDED.value not in ymir_labels:
                    existing_keys.add(issue_key)
                    logger.info(f"Issue {issue_key} has Ymir labels {ymir_labels} - marking as existing")
                elif JiraLabels.RETRY_NEEDED.value in ymir_labels:
                    if has_in_progress:
                        # Don't re-enqueue a retry-needed issue that's already running.
                        existing_keys.add(issue_key)
                        logger.info(
                            f"Issue {issue_key} has {JiraLabels.RETRY_NEEDED.value} "
                            "but is already in progress - skipping"
                        )
                    else:
                        logger.info(
                            f"Issue {issue_key} has {JiraLabels.RETRY_NEEDED.value} - marking for retry"
                        )
                        remove_issues_for_retry.add(issue_key)
                        retry_needed_keys.add(issue_key)

            pushed_count = 0
            skipped_count = 0
            ignored_count = 0
            modular_count = 0

            for issue in issues:
                try:
                    if self.max_issues is not None and pushed_count >= self.max_issues:
                        logger.info(f"Reached MAX_ISSUES limit ({self.max_issues})")
                        break

                    issue_key = issue["key"]
                    fields = issue.get("fields") or {}

                    downstream_component = fields.get("customfield_10669") or ""
                    if self.MODULAR_COMPONENT_PATTERN.match(downstream_component):
                        logger.info(f"Skipping issue {issue_key} - modular issue: {downstream_component}")
                        modular_count += 1
                        continue

                    if self.ignored_components:
                        components = {
                            name.lower() for c in (fields.get("components") or []) if (name := c.get("name"))
                        }
                        if components & self.ignored_components:
                            logger.info(
                                f"Skipping issue {issue_key} - has ignored component(s):"
                                f" {components & self.ignored_components}"
                            )
                            ignored_count += 1
                            continue

                    if issue_key in existing_keys and issue_key not in remove_issues_for_retry:
                        logger.debug(f"Skipping issue {issue_key} - already exists in triage_queue")
                        skipped_count += 1
                        continue

                    user_triggered = issue_key in user_triggered_keys
                    retry_needed = issue_key in retry_needed_keys

                    # For ymir_todo and ymir_retry_needed issues, atomically swap
                    # the trigger label for ymir_triage_in_progress before enqueueing.
                    # This dedupes against the very next sweep (which will see the
                    # in-progress marker and skip), and consumes the trigger so a
                    # stuck run doesn't loop. If this write fails after retries, do
                    # NOT push to the queue — otherwise the issue would be picked up
                    # again on the next sweep without the in-progress marker.
                    label_to_consume = None
                    if user_triggered:
                        label_to_consume = JiraLabels.TODO.value
                    elif retry_needed:
                        label_to_consume = JiraLabels.RETRY_NEEDED.value

                    if label_to_consume:
                        if self.dry_run:
                            logger.info(
                                f"DRY_RUN: would flip {label_to_consume} → "
                                f"{JiraLabels.TRIAGE_IN_PROGRESS.value} on {issue_key}; "
                                f"skipping Jira write but proceeding with Redis push"
                            )
                        else:
                            try:
                                self._edit_jira_labels(
                                    issue_key,
                                    add=[JiraLabels.TRIAGE_IN_PROGRESS.value],
                                    remove=[label_to_consume],
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to flip {label_to_consume} → "
                                    f"{JiraLabels.TRIAGE_IN_PROGRESS.value} on {issue_key} "
                                    f"after retries; skipping enqueue to avoid duplicate processing: {e}"
                                )
                                continue

                    # Create task using shared Pydantic model
                    task = Task.from_issue(issue_key, user_triggered=user_triggered)

                    await fix_await(redis_conn.lpush(RedisQueues.TRIAGE_QUEUE.value, task.to_json()))
                    pushed_count += 1

                    # Add to existing_keys to avoid duplicates within this batch
                    existing_keys.add(issue_key)

                    logger.debug(f"Pushed issue {issue_key} to triage_queue")

                except Exception as e:
                    logger.error(f"Error pushing issue {issue.get('key', 'unknown')} to queue: {e}")
                    continue

            logger.info(f"Successfully pushed {pushed_count}/{len(issues)} issues to triage_queue")
            if skipped_count > 0:
                logger.info(f"Skipped {skipped_count} issues that already exist in queue")
            if ignored_count > 0:
                logger.info(f"Skipped {ignored_count} issues due to ignored components")
            if modular_count > 0:
                logger.info(f"Skipped {modular_count} modular issues")
            return pushed_count

    async def run(self) -> None:
        try:
            logger.info("Starting Jira issue fetcher")

            issues = await self.search_issues()

            if not issues:
                logger.info("No issues found matching the query")
                return

            pushed_count = await self.push_issues_to_queue(issues)

            logger.info(f"Completed: {pushed_count} issues added to triage_queue")

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
