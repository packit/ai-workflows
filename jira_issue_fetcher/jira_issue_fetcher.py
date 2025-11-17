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

Configuration:
- QUERY: Custom JQL query (default: "project=RHEL and assignee = jotnar-project")
- ONLY_POSTPONED_RETRY: If set to "true", "1", or "yes", only issues with
  jotnar_postponed label will be removed from queues for retry. This mode is
  intended for a secondary instance that runs less frequently to handle
  postponed issues.
"""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Any
from urllib.parse import urljoin

import redis.asyncio as redis
import requests
import backoff

from common.models import (
    Task,
    TriageInputSchema,
    RebaseInputSchema,
    BackportInputSchema,
    RebaseOutputSchema,
    BackportOutputSchema,
    ClarificationNeededData,
    NoActionData,
    ErrorData
)
from common.utils import redis_client, fix_await
from common.constants import JiraLabels, RedisQueues

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)




class JiraIssueFetcher:

    DEFAULT_QUERY = "project=RHEL and assignee = jotnar-project"
    MAX_RESULTS_PER_PAGE = 500  # Optimize for fewer, more expensive calls
    RATE_LIMIT_CALLS_PER_SECOND = 5
    RATE_LIMIT_DELAY = 1.0 / RATE_LIMIT_CALLS_PER_SECOND  # 0.2 seconds between calls
    API_TIMEOUT = 90  # 90 seconds timeout

    def __init__(self):
        self.jira_url = os.environ["JIRA_URL"]
        self.jira_token = os.environ["JIRA_TOKEN"]
        self.redis_url = os.environ["REDIS_URL"]

        # Allow query override from environment
        self.query = os.getenv("QUERY", self.DEFAULT_QUERY)

        # Use constant page size
        self.max_results_per_page = self.MAX_RESULTS_PER_PAGE

        # Configuration for retry behavior: if True, only remove issues for retry when they have POSTPONED label
        self.only_postponed_retry = os.getenv("ONLY_POSTPONED_RETRY", "false").lower() in ("true", "1", "yes")

        self.headers = {
            "Authorization": f"Bearer {self.jira_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Rate limiting
        self.last_request_time = 0.0

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
        logger=logger
    )
    def _make_request_with_retries(self, url: str, json_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Make HTTP request with exponential backoff retries
        """
        response = requests.post(
            url,
            json=json_data,
            headers=self.headers,
            timeout=self.API_TIMEOUT
        )

        # Handle rate limiting specifically
        if response.status_code == 429:
            logger.warning("Rate limited (429), will retry with backoff")
            raise requests.HTTPError("Rate limited", response=response)

        response.raise_for_status()
        return response.json()

    async def search_issues(self) -> List[Dict[str, Any]]:
        """
        Search for issues using the configured query with pagination
        """
        logger.info(f"Starting issue search with query: {self.query}")

        all_issues = []
        start_at = 0
        total_issues = None

        fields = [
            "key",        # Issue key (e.g., RHEL-12345)
            "labels",     # Issue labels
        ]

        while True:
            await self._rate_limit()

            # Use POST with JSON payload instead of GET with params to handle long queries
            json_payload = {
                "jql": self.query,
                "startAt": start_at,
                "maxResults": self.max_results_per_page,
                "fields": fields
            }

            logger.info(f"Fetching issues: startAt={start_at}, maxResults={self.max_results_per_page}")

            try:
                url = urljoin(self.jira_url, "rest/api/2/search")
                response_data = self._make_request_with_retries(url, json_data=json_payload)

                issues = response_data.get("issues", [])
                all_issues.extend(issues)

                if total_issues is None:
                    total_issues = response_data.get("total", 0)
                    logger.info(f"Total issues found: {total_issues}")

                logger.info(f"Retrieved {len(issues)} issues (total so far: {len(all_issues)}/{total_issues})")

                if len(all_issues) >= total_issues or len(issues) == 0:
                    break

                start_at += self.max_results_per_page

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
                                        case RedisQueues.REBASE_QUEUE_C9S.value | RedisQueues.REBASE_QUEUE_C10S.value | RedisQueues.BACKPORT_QUEUE_C9S.value | RedisQueues.BACKPORT_QUEUE_C10S.value | RedisQueues.CLARIFICATION_NEEDED_QUEUE.value | RedisQueues.BACKPORT_QUEUE.value | RedisQueues.REBASE_QUEUE.value:
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
                                        case RedisQueues.NO_ACTION_LIST.value:
                                            schema = NoActionData.model_validate_json(item)
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

    async def push_issues_to_queue(self, issues: List[Dict[str, Any]]) -> int:
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
            # Extend existing_keys with issues that have jötnar labels (except jotnar_retry_needed)
            for issue in issues:
                issue_key = issue.get("key")
                if issue_key:
                    fields = issue.get("fields", {})
                    labels = fields.get("labels", [])
                    jotnar_labels = [label for label in labels if label.startswith('jotnar_')]

                    # If issue has jötnar labels and there is no jotnar_retry_needed label, mark as existing
                    if jotnar_labels and JiraLabels.RETRY_NEEDED.value not in jotnar_labels:
                        existing_keys.add(issue_key)
                        logger.info(f"Issue {issue_key} has jötnar labels {jotnar_labels} - marking as existing")
                    elif JiraLabels.RETRY_NEEDED.value in jotnar_labels:
                        logger.info(f"Issue {issue_key} has jotnar_retry_needed label - marking for retry")
                        remove_issues_for_retry.add(issue_key)
                    elif JiraLabels.POSTPONED.value in jotnar_labels and self.only_postponed_retry:
                        logger.info(f"Issue {issue_key} has jotnar_postponed label - re-processing it")
                        remove_issues_for_retry.add(issue_key)
                    elif not jotnar_labels:
                        logger.info(f"Issue {issue_key} has no jötnar labels - marking for retry")
                        remove_issues_for_retry.add(issue_key)

            pushed_count = 0
            skipped_count = 0

            for issue in issues:
                try:
                    issue_key = issue["key"]

                    if issue_key in existing_keys - remove_issues_for_retry:
                        logger.debug(f"Skipping issue {issue_key} - already exists in triage_queue")
                        skipped_count += 1
                        continue

                    # Create task using shared Pydantic model
                    task = Task.from_issue(issue_key)

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
            return pushed_count

    async def run(self) -> None:
        try:
            mode_str = "POSTPONED-only retry mode" if self.only_postponed_retry else "default retry mode"
            logger.info(f"Starting Jira issue fetcher ({mode_str})")

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
    required_vars = ["JIRA_URL", "JIRA_TOKEN", "REDIS_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.info("Required environment variables:")
        logger.info("  JIRA_URL - Jira instance URL (e.g., https://issues.redhat.com)")
        logger.info("  JIRA_TOKEN - Jira authentication token")
        logger.info("  REDIS_URL - Redis connection URL (e.g., redis://localhost:6379)")
        sys.exit(1)

    if os.getenv("QUERY"):
        logger.info("Using QUERY from environment variable")

    if os.getenv("ONLY_POSTPONED_RETRY"):
        logger.info("ONLY_POSTPONED_RETRY is set - using POSTPONED-only retry mode")

    asyncio.run(main())
