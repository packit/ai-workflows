#!/usr/bin/env python3
"""Post an agent-generated merge request review as a GitLab note.

The privileged executor half of the safe-outputs split used by the
agentic MR review in osci-pipelines (rog/): this is the only job of
the review pipeline that holds a GitLab token. It reads the JSON
review record the agent emitted through the ``safe-output review``
tool (``safe-outputs/review.json``, ``{"type": "review", "body": ...}``)
and publishes the ``## Summary``-onward body as an MR note.

Posting is idempotent by editing our previous note in place (PUT)
rather than deleting it — GitLab has no "minimize" for notes, and
in-place edits preserve history in the note's edit log.

Every posted note ends with an enforced provenance trailer —
hyperlinks to the implementation (``MR_REVIEW_IMPL_URL``, the agenthome
image, and the run logs) — appended by this executor rather than
trusted to the agent, the same attribution discipline as gh-aw safe
outputs. GitLab API calls use backoff so rate limits cannot fail the
job.

Exits 0 with a message when the review artifact is missing (e.g. the
review job failed; it is allow_failure) and 1 on a malformed record.
"""

import json
import logging
import os
import sys

import backoff
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

MARKER = "### Agentic review"
# Notes posted under earlier names of this bot, still replaced for
# idempotency so retries don't leave stale duplicates behind.
LEGACY_MARKERS = ["### opencode AI review"]

REVIEW_FILE = "safe-outputs/review.json"

_backoff_retry = backoff.on_exception(
    backoff.expo,
    (requests.RequestException,),
    max_tries=5,
    jitter=backoff.full_jitter,
)


class GitLab:
    def __init__(self, api_url: str, token: str):
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["PRIVATE-TOKEN"] = token

    @_backoff_retry
    def get(self, path: str, params: dict | None = None):
        resp = self.session.get(f"{self.api_url}{path}", params=params, timeout=60)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    @_backoff_retry
    def post(self, path: str, body: dict) -> dict:
        resp = self.session.post(f"{self.api_url}{path}", json=body, timeout=60)
        resp.raise_for_status()
        return resp.json()

    @_backoff_retry
    def put(self, path: str, body: dict) -> dict:
        resp = self.session.put(f"{self.api_url}{path}", json=body, timeout=60)
        resp.raise_for_status()
        return resp.json()


def load_review(path: str) -> dict | None:
    """The agent's review as a JSON safe-output record, or None when absent.

    Raises ValueError on a record that is present but malformed.
    """
    try:
        with open(path) as f:
            record = json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON ({e}); was the artifact truncated?") from e
    if (
        not isinstance(record, dict)
        or record.get("type") != "review"
        or not isinstance(record.get("body"), str)
    ):
        raise ValueError(f"malformed review record in {path}")
    return record


def review_body(record: dict) -> str:
    """Publish from "## Summary" onward; fall back to the whole body."""
    lines = record["body"].splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Summary":
            return "\n".join(lines[i:]).strip()
    return record["body"].strip()


def build_trailer() -> str:
    """Provenance trailer: implementation, agenthome image, run logs."""
    parts = []
    impl = os.environ.get("MR_REVIEW_IMPL_URL", "")
    if impl:
        parts.append(impl)
    image = os.environ.get("AGENTHOME_IMAGE", "")
    if image:
        parts.append(f"agenthome image `{image}`")
    job = os.environ.get("CI_JOB_URL", "")
    if job:
        parts.append(f"[run logs]({job})")
    return "*Agentic review — " + " · ".join(parts) + "*"


def compose_note(record: dict) -> str:
    return f"{MARKER}\n\n{review_body(record)}\n\n---\n{build_trailer()}"


def find_own_note(gitlab: GitLab, project: str, mr_iid: str, username: str) -> dict | None:
    notes = gitlab.get(f"/projects/{project}/merge_requests/{mr_iid}/notes", {"per_page": 100})
    for note in notes:
        if note["author"]["username"] == username and any(
            note["body"].startswith(m) for m in [MARKER, *LEGACY_MARKERS]
        ):
            return note
    return None


def main() -> int:
    api_url = os.environ.get("CI_API_V4_URL", "")
    project = os.environ.get("CI_PROJECT_ID", "")
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID", "")
    token = os.environ.get("GITLAB_TOKEN", "")
    if not (api_url and project and mr_iid):
        print("CI_API_V4_URL, CI_PROJECT_ID and CI_MERGE_REQUEST_IID must be set", file=sys.stderr)
        return 1

    try:
        record = load_review(REVIEW_FILE)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if record is None:
        logger.info("no %s artifact; nothing to post", REVIEW_FILE)
        return 0

    body = review_body(record)
    if not body:
        print("review body is empty", file=sys.stderr)
        return 1
    note = compose_note(record)

    if not token:
        print("GITLAB_TOKEN not set; review available as artifact only")
        return 0

    gitlab = GitLab(api_url, token)
    me = gitlab.get("/user")["username"]
    existing = find_own_note(gitlab, project, mr_iid, me)
    if existing:
        gitlab.put(f"/projects/{project}/merge_requests/{mr_iid}/notes/{existing['id']}", {"body": note})
        logger.info("edited previous note %s in place", existing["id"])
    else:
        gitlab.post(f"/projects/{project}/merge_requests/{mr_iid}/notes", {"body": note})
        logger.info("review posted to MR !%s as %s", mr_iid, me)
    return 0


if __name__ == "__main__":
    sys.exit(main())
