#!/usr/bin/env python3
"""
MR Cleanup Script

Phase 1 — Stale MR cleanup:
  Scans open GitLab merge requests authored by Ymir bots and closes those
  whose referenced Jira issues have all been closed.  No Jira labels are
  modified — metrics dashboards depend on those labels remaining in place.

Phase 2 — Closed-MR Jira reset:
  Scans closed (not merged) bot MRs and resets the corresponding
  Jira labels so the issues accurately reflect that no active MR exists.
  Removes automation outcome labels (ymir_backported, ymir_triaged_backport,
  etc.) and adds ymir_mr_closed for tracking.  Does NOT add ymir_retry_needed
  — re-processing requires manual intervention (ymir_todo).
"""

import base64
import enum
import logging
import os
import re
import sys
import time
import typing
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
DEFAULT_BOT_AUTHORS = "jotnar-bot,redhat-ymir-agent"
PER_PAGE = 100

JIRA_CLOSED_STATUS = "Closed"
JIRA_BATCH_SIZE = 100

RATE_LIMIT_DELAY = 0.2
REQUEST_TIMEOUT = 90

CLOSE_NOTE_MARKER = "Closing this merge request"

JIRA_MR_CLOSED_LABEL = "ymir_mr_closed"
JIRA_RESET_MR_LABEL = "ymir_jiras_cleaned_up"


class Action(enum.Enum):
    CLOSED = "closed"
    SKIPPED_NO_JIRA = "skipped_no_jira"
    SKIPPED_OPEN_JIRAS = "skipped_open_jiras"
    SKIPPED_ALREADY_CLEANED = "skipped_already_cleaned"
    ERRORED = "errored"

    JIRA_RESET = "jira_reset"
    SKIPPED_ALREADY_RESET = "skipped_already_reset"


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
        self.close_stale_mrs = os.getenv("CLOSE_STALE_MRS", "true").lower() == "true"
        self.reset_closed_mr_jiras = os.getenv("RESET_CLOSED_MR_JIRAS", "true").lower() == "true"
        self.bot_authors = [
            a.strip() for a in os.getenv("GITLAB_BOT_AUTHORS", DEFAULT_BOT_AUTHORS).split(",")
        ]

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
            logger.info("DRY_RUN=true — no changes will be made")

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

    @_backoff_retry
    def _jira_put(self, path: str, json: dict):
        self._rate_limit()
        url = f"{self.jira_url}/rest/api/3/{path}"
        resp = self.jira_session.put(url, json=json, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

    def fetch_open_mrs(self) -> list[dict]:
        all_mrs = []
        for group in GITLAB_GROUPS:
            for author in self.bot_authors:
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
            if note.get("author") and note["author"].get("username", "") in self.bot_authors
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

    def fetch_closed_mrs(self) -> list[dict]:
        all_mrs = []
        for group in GITLAB_GROUPS:
            for author in self.bot_authors:
                logger.info("Fetching closed MRs in %s by %s", group, author)
                encoded_group = urlquote(group, safe="")
                page = 1
                while True:
                    mrs = self._gitlab_get(
                        f"groups/{encoded_group}/merge_requests",
                        params={
                            "state": "closed",
                            "author_username": author,
                            "per_page": PER_PAGE,
                            "page": page,
                            "include_subgroups": "true",
                            "not[labels]": JIRA_RESET_MR_LABEL,
                        },
                    )
                    if not mrs:
                        break
                    all_mrs.extend(mrs)
                    page += 1

        logger.info("Found %d closed MRs to check for Jira reset", len(all_mrs))
        return all_mrs

    def fetch_jira_labels(self, issue_keys: set[str]) -> dict[str, list[str]]:
        if not issue_keys:
            return {}

        labels: dict[str, list[str]] = {}
        keys_list = sorted(issue_keys)

        for i in range(0, len(keys_list), JIRA_BATCH_SIZE):
            batch = keys_list[i : i + JIRA_BATCH_SIZE]
            jql = f"key in ({','.join(batch)})"
            try:
                data = self._jira_post(
                    "search/jql",
                    json={"jql": jql, "maxResults": len(batch), "fields": ["labels"]},
                )
                for issue in data.get("issues", []):
                    key = issue["key"]
                    labels[key] = issue["fields"].get("labels", [])
            except Exception:
                logger.exception("Failed to fetch Jira labels for batch starting at %d", i)

        missing = issue_keys - labels.keys()
        if missing:
            logger.warning("Could not fetch labels for Jira issues: %s", ", ".join(sorted(missing)))

        return labels

    JIRA_LABELS_TO_PRESERVE: typing.ClassVar[set[str]] = {
        JIRA_MR_CLOSED_LABEL,
        "ymir_todo",
        "ymir_retry_needed",
    }

    def _reset_jira_labels(self, issue_key: str, current_labels: list[str]):
        ymir_labels_to_remove = [
            label
            for label in current_labels
            if label.startswith("ymir_") and label not in self.JIRA_LABELS_TO_PRESERVE
        ]
        already_has_closed = JIRA_MR_CLOSED_LABEL in current_labels

        if not ymir_labels_to_remove and already_has_closed:
            return False

        update_ops = [{"remove": label} for label in ymir_labels_to_remove]
        if not already_has_closed:
            update_ops.append({"add": JIRA_MR_CLOSED_LABEL})

        if self.dry_run:
            logger.info(
                "DRY_RUN: would update Jira %s — remove %s, add %s",
                issue_key,
                ymir_labels_to_remove,
                [] if already_has_closed else [JIRA_MR_CLOSED_LABEL],
            )
            return True

        self._jira_put(f"issue/{issue_key}", json={"update": {"labels": update_ops}})
        logger.info(
            "Reset Jira %s — removed %s, added %s",
            issue_key,
            ymir_labels_to_remove,
            [] if already_has_closed else [JIRA_MR_CLOSED_LABEL],
        )
        return True

    def process_rejected_mr(
        self,
        mr: dict,
        jira_keys: set[str],
        jira_labels: dict[str, list[str]],
        skip_jira_keys: set[str],
    ) -> tuple[Action, set[str]]:
        mr_url = mr["web_url"]
        reset_keys: set[str] = set()

        if not jira_keys:
            logger.info("No Jira keys found in closed MR %s — marking as processed", mr_url)
            self._add_label(mr, JIRA_RESET_MR_LABEL)
            return Action.SKIPPED_NO_JIRA, reset_keys

        for key in sorted(jira_keys):
            if key in skip_jira_keys:
                logger.info("Skipping %s — still referenced by an open MR or already reset", key)
                continue
            current_labels = jira_labels.get(key)
            if current_labels is None:
                logger.warning("Could not fetch labels for %s (MR %s) — skipping issue", key, mr_url)
                continue
            try:
                if self._reset_jira_labels(key, current_labels):
                    reset_keys.add(key)
            except Exception:
                logger.exception("Failed to reset labels for %s (MR %s)", key, mr_url)

        self._add_label(mr, JIRA_RESET_MR_LABEL)

        if reset_keys:
            return Action.JIRA_RESET, reset_keys

        return Action.SKIPPED_ALREADY_RESET, reset_keys

    def run(self):
        logger.info("Starting MR cleanup")

        active_jira_keys: set[str] = set()
        if self.close_stale_mrs:
            mrs = self.fetch_open_mrs()
            if mrs:
                active_jira_keys = self._run_stale_mr_cleanup(mrs)
            else:
                logger.info("No open MRs found — skipping phase 1")
        else:
            logger.info("CLOSE_STALE_MRS=false — skipping phase 1")
            if self.reset_closed_mr_jiras:
                active_jira_keys = self._collect_open_mr_jira_keys()

        if self.reset_closed_mr_jiras:
            closed_mrs = self.fetch_closed_mrs()
            if closed_mrs:
                self._run_rejected_mr_cleanup(closed_mrs, active_jira_keys)
            else:
                logger.info("No closed MRs to process — skipping phase 2")
        else:
            logger.info("RESET_CLOSED_MR_JIRAS=false — skipping phase 2")

    def _extract_all_jira_keys(
        self, mrs: list[dict], label: str = ""
    ) -> tuple[dict[int, set[str]], set[str]]:
        prefix = f"{label} " if label else ""
        logger.info("Extracting Jira keys from %d %sMRs", len(mrs), prefix)
        mr_jira_keys: dict[int, set[str]] = {}
        all_keys: set[str] = set()
        for i, mr in enumerate(mrs, 1):
            if i % 25 == 0 or i == len(mrs):
                logger.info("  Processing %sMR %d/%d", prefix, i, len(mrs))
            try:
                keys = self.extract_jira_keys_from_mr(mr)
            except Exception:
                logger.exception("Failed to extract Jira keys for MR %s", mr.get("web_url", mr.get("id")))
                continue
            mr_jira_keys[mr["id"]] = keys
            all_keys.update(keys)
        logger.info("Found %d unique Jira keys across %sMRs", len(all_keys), prefix)
        return mr_jira_keys, all_keys

    def _collect_open_mr_jira_keys(self) -> set[str]:
        mrs = self.fetch_open_mrs()
        if not mrs:
            return set()
        _, all_keys = self._extract_all_jira_keys(mrs, label="open")
        return all_keys

    def _run_stale_mr_cleanup(self, mrs: list[dict]) -> set[str]:
        """Returns the set of Jira keys still referenced by open (not-closed) MRs."""
        mr_jira_keys, all_keys = self._extract_all_jira_keys(mrs)

        # Batch-query Jira statuses
        jira_statuses = self.fetch_jira_statuses(all_keys)
        logger.info("Fetched statuses for %d Jira issues", len(jira_statuses))

        # Process each MR
        closed_keys: set[str] = set()
        results: dict[Action, list[str]] = {}
        for mr in mrs:
            try:
                keys = mr_jira_keys.get(mr["id"])
                action = Action.ERRORED if keys is None else self.process_mr(mr, keys, jira_statuses)
                if action == Action.CLOSED and keys:
                    closed_keys.update(keys)
            except Exception:
                logger.exception("Failed to process MR %s", mr.get("web_url", mr.get("id")))
                action = Action.ERRORED
            results.setdefault(action, []).append(mr["web_url"])

        summary = {action.value: len(urls) for action, urls in results.items()}
        logger.info("Stale MR cleanup complete: %s", summary)
        return all_keys - closed_keys

    def _run_rejected_mr_cleanup(self, closed_mrs: list[dict], active_jira_keys: set[str]):
        mr_jira_keys, all_keys = self._extract_all_jira_keys(closed_mrs, label="closed")

        jira_labels = self.fetch_jira_labels(all_keys)
        logger.info("Fetched labels for %d Jira issues", len(jira_labels))

        skip_jira_keys = set(active_jira_keys)
        results: dict[Action, list[str]] = {}
        for mr in closed_mrs:
            try:
                keys = mr_jira_keys.get(mr["id"])
                if keys is None:
                    action = Action.ERRORED
                else:
                    action, reset_keys = self.process_rejected_mr(mr, keys, jira_labels, skip_jira_keys)
                    skip_jira_keys.update(reset_keys)
            except Exception:
                logger.exception("Failed to process closed MR %s", mr.get("web_url", mr.get("id")))
                action = Action.ERRORED
            results.setdefault(action, []).append(mr["web_url"])

        summary = {action.value: len(urls) for action, urls in results.items()}
        logger.info("Rejected MR cleanup complete: %s", summary)


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
