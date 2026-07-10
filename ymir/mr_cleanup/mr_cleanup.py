#!/usr/bin/env python3
"""
MR Cleanup Script

Scans open GitLab merge requests authored by Ymir bots and closes those
whose referenced Jira issues have all been closed.

No Jira labels are modified — metrics dashboards depend on those labels
remaining in place.
"""

import base64
import enum
import logging
import os
import re
import sys
import time
from urllib.parse import quote as urlquote

import backoff
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

GITLAB_API_URL = "https://gitlab.com/api/v4"
GITLAB_GROUPS = ["redhat/rhel/rpms", "redhat/centos-stream/rpms"]
GITLAB_BOT_AUTHORS = ["jotnar-bot", "redhat-ymir-agent"]
PER_PAGE = 100

JIRA_CLOSED_STATUS = "Closed"
JIRA_BATCH_SIZE = 100

RATE_LIMIT_DELAY = 0.2
REQUEST_TIMEOUT = 90

CLOSE_NOTE_MARKER = "Closing this merge request"


class Action(enum.Enum):
    CLOSED = "closed"
    SKIPPED_NO_JIRA = "skipped_no_jira"
    SKIPPED_OPEN_JIRAS = "skipped_open_jiras"
    SKIPPED_ALREADY_CLEANED = "skipped_already_cleaned"
    ERRORED = "errored"


def _giveup_on_permanent(e):
    return (
        isinstance(e, requests.HTTPError)
        and e.response is not None
        and e.response.status_code in (401, 403, 404)
    )


_backoff_retry = backoff.on_exception(
    backoff.expo,
    (requests.RequestException,),
    max_tries=4,
    base=2,
    giveup=_giveup_on_permanent,
)


class MRCleanup:
    def __init__(self):
        self.gitlab_token = os.environ["GITLAB_TOKEN"]
        self.jira_url = os.environ["JIRA_URL"].rstrip("/")
        self.dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
        self.target_mr = os.getenv("TARGET_MR", "")

        self.gitlab_session = requests.Session()
        self.gitlab_session.headers.update(
            {
                "Authorization": f"Bearer {self.gitlab_token}",
                "Content-Type": "application/json",
            }
        )

        jira_email = os.environ["JIRA_EMAIL"]
        jira_token = os.environ["JIRA_TOKEN"]
        credentials = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
        self.jira_session = requests.Session()
        self.jira_session.headers.update(
            {
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self.last_request_time = 0.0

        if self.dry_run:
            logger.info("DRY_RUN=true — no MRs will be closed or commented on")

    def _rate_limit(self):
        current_time = time.time()
        elapsed = current_time - self.last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self.last_request_time = time.time()

    @_backoff_retry
    def _gitlab_get(self, path: str, params: dict | None = None) -> list | dict:
        self._rate_limit()
        url = f"{GITLAB_API_URL}/{path}"
        resp = self.gitlab_session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    @_backoff_retry
    def _gitlab_post(self, path: str, json: dict | None = None) -> dict:
        self._rate_limit()
        url = f"{GITLAB_API_URL}/{path}"
        resp = self.gitlab_session.post(url, json=json, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    @_backoff_retry
    def _gitlab_put(self, path: str, json: dict | None = None) -> dict:
        self._rate_limit()
        url = f"{GITLAB_API_URL}/{path}"
        resp = self.gitlab_session.put(url, json=json, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    @_backoff_retry
    def _jira_post(self, path: str, json: dict) -> dict:
        self._rate_limit()
        url = f"{self.jira_url}/rest/api/3/{path}"
        resp = self.jira_session.post(url, json=json, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def fetch_open_mrs(self) -> list[dict]:
        all_mrs = []
        for group in GITLAB_GROUPS:
            for author in GITLAB_BOT_AUTHORS:
                logger.info("Fetching open MRs in %s by %s", group, author)
                encoded_group = urlquote(group, safe="")
                page = 1
                while True:
                    mrs = self._gitlab_get(
                        f"groups/{encoded_group}/merge_requests",
                        params={
                            "state": "opened",
                            "author_username": author,
                            "per_page": PER_PAGE,
                            "page": page,
                            "include_subgroups": "true",
                        },
                    )
                    if not mrs:
                        break
                    all_mrs.extend(mrs)
                    page += 1

        if self.target_mr:
            all_mrs = [mr for mr in all_mrs if mr["web_url"] == self.target_mr]
            if not all_mrs:
                logger.warning("TARGET_MR %s not found among open bot MRs", self.target_mr)

        logger.info("Found %d open MRs total", len(all_mrs))
        return all_mrs

    def extract_jira_keys_from_mr(self, mr: dict) -> set[str]:
        project_id = mr["project_id"]
        iid = mr["iid"]
        commits = self._gitlab_get(
            f"projects/{project_id}/merge_requests/{iid}/commits",
            params={"per_page": PER_PAGE},
        )
        keys: set[str] = set()
        for commit in commits:
            message = commit.get("message", "")
            for line in message.split("\n"):
                if "resolves:" in line.lower():
                    keys.update(re.findall(r"RHEL-\d+", line))
        return keys

    def fetch_jira_statuses(self, issue_keys: set[str]) -> dict[str, str]:
        if not issue_keys:
            return {}

        statuses: dict[str, str] = {}
        keys_list = sorted(issue_keys)

        for i in range(0, len(keys_list), JIRA_BATCH_SIZE):
            batch = keys_list[i : i + JIRA_BATCH_SIZE]
            jql = f"key in ({','.join(batch)})"
            try:
                data = self._jira_post(
                    "search/jql",
                    json={"jql": jql, "maxResults": len(batch), "fields": ["status"]},
                )
                for issue in data.get("issues", []):
                    key = issue["key"]
                    status = issue["fields"]["status"]["name"]
                    statuses[key] = status
            except Exception:
                logger.exception("Failed to fetch Jira statuses for batch starting at %d", i)

        missing = issue_keys - statuses.keys()
        if missing:
            logger.warning("Could not fetch status for Jira issues: %s", ", ".join(sorted(missing)))

        return statuses

    def _fetch_bot_notes(self, mr: dict) -> list[str]:
        """Return bodies of all bot-authored notes on the MR."""
        project_id = mr["project_id"]
        iid = mr["iid"]
        notes = self._gitlab_get(
            f"projects/{project_id}/merge_requests/{iid}/notes",
            params={"per_page": PER_PAGE, "order_by": "created_at", "sort": "desc"},
        )
        return [
            note.get("body", "")
            for note in notes
            if note.get("author") and note["author"].get("username", "") in GITLAB_BOT_AUTHORS
        ]

    def _mr_has_existing_close_note(self, bot_notes: list[str]) -> bool:
        return any(CLOSE_NOTE_MARKER in body for body in bot_notes)

    def _post_mr_note(self, mr: dict, body: str):
        project_id = mr["project_id"]
        iid = mr["iid"]
        if self.dry_run:
            logger.info("DRY_RUN: would post note on %s: %s", mr["web_url"], body)
            return
        self._gitlab_post(
            f"projects/{project_id}/merge_requests/{iid}/notes",
            json={"body": body},
        )

    def _close_mr(self, mr: dict):
        project_id = mr["project_id"]
        iid = mr["iid"]
        if self.dry_run:
            logger.info("DRY_RUN: would close MR %s", mr["web_url"])
            return
        self._gitlab_put(
            f"projects/{project_id}/merge_requests/{iid}",
            json={"state_event": "close"},
        )

    def _add_label(self, mr: dict, label: str):
        project_id = mr["project_id"]
        iid = mr["iid"]
        if label in mr.get("labels", []):
            return
        if self.dry_run:
            logger.info("DRY_RUN: would add label '%s' to MR %s", label, mr["web_url"])
            return
        self._gitlab_put(
            f"projects/{project_id}/merge_requests/{iid}",
            json={"add_labels": label},
        )

    def process_mr(self, mr: dict, jira_keys: set[str], jira_statuses: dict[str, str]) -> Action:
        mr_url = mr["web_url"]

        if "ymir_cleaned_up" in mr.get("labels", []):
            logger.debug("MR %s already has ymir_cleaned_up label — skipping", mr_url)
            return Action.SKIPPED_ALREADY_CLEANED

        if not jira_keys:
            logger.info("No Jira keys found in MR %s — skipping", mr_url)
            return Action.SKIPPED_NO_JIRA

        closed_keys = {k for k in jira_keys if jira_statuses.get(k) == JIRA_CLOSED_STATUS}
        open_keys = jira_keys - closed_keys

        if not closed_keys:
            logger.debug("All Jiras still open for MR %s (%s)", mr_url, ", ".join(sorted(jira_keys)))
            return Action.SKIPPED_OPEN_JIRAS

        # Some Jiras still open — skip for now (potentially in future: comment about partial-closure)
        if open_keys:
            return Action.SKIPPED_OPEN_JIRAS

        bot_notes = self._fetch_bot_notes(mr)
        keys_str = ", ".join(sorted(closed_keys))
        logger.info("All Jiras closed for MR %s (%s) — closing", mr_url, keys_str)

        if not self._mr_has_existing_close_note(bot_notes):
            self._post_mr_note(
                mr,
                f"Closing this merge request — all referenced Jira issues "
                f"({keys_str}) have been closed. "
                f"If this MR is still needed, reopen it (the `ymir_cleaned_up` label "
                f"prevents repeated closure) or reach out in "
                f"[#forum-ymir-package-automation](https://redhat.enterprise.slack.com/archives/C095699FLMR).",
            )
        self._close_mr(mr)
        self._add_label(mr, "ymir_cleaned_up")
        return Action.CLOSED

    def run(self):
        logger.info("Starting MR cleanup")

        mrs = self.fetch_open_mrs()
        if not mrs:
            logger.info("No open MRs found — nothing to do")
            return

        # Collect all Jira keys referenced across all MRs
        logger.info("Extracting Jira keys from %d MRs", len(mrs))
        mr_jira_keys: dict[int, set[str]] = {}
        all_keys: set[str] = set()
        for i, mr in enumerate(mrs, 1):
            if i % 25 == 0 or i == len(mrs):
                logger.info("  Processing MR %d/%d", i, len(mrs))
            try:
                keys = self.extract_jira_keys_from_mr(mr)
            except Exception:
                logger.exception("Failed to extract Jira keys for MR %s", mr.get("web_url", mr.get("id")))
                continue
            mr_jira_keys[mr["id"]] = keys
            all_keys.update(keys)

        logger.info("Found %d unique Jira keys across all MRs", len(all_keys))

        # Batch-query Jira statuses
        jira_statuses = self.fetch_jira_statuses(all_keys)
        logger.info("Fetched statuses for %d Jira issues", len(jira_statuses))

        # Process each MR
        results: dict[Action, list[str]] = {}
        for mr in mrs:
            try:
                keys = mr_jira_keys.get(mr["id"])
                action = Action.ERRORED if keys is None else self.process_mr(mr, keys, jira_statuses)
            except Exception:
                logger.exception("Failed to process MR %s", mr.get("web_url", mr.get("id")))
                action = Action.ERRORED
            results.setdefault(action, []).append(mr["web_url"])

        summary = {action.value: len(urls) for action, urls in results.items()}
        logger.info("MR cleanup complete: %s", summary)
        if self.dry_run and (urls := results.get(Action.CLOSED)):
            logger.info("DRY_RUN — would close %d MRs:", len(urls))
            for url in urls:
                logger.info("  %s", url)


def main():
    try:
        cleanup = MRCleanup()
        cleanup.run()
    except Exception:
        logger.exception("MR cleanup failed")
        sys.exit(1)


if __name__ == "__main__":
    required_vars = ["GITLAB_TOKEN", "JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN"]
    missing = [v for v in required_vars if not os.getenv(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    main()
