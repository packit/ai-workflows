#!/usr/bin/env python3
"""
Ymir CVE Activity CLI — measure Ymir's effectiveness at resolving Jira issues.

Collects data from GitLab and Jira APIs, produces summary reports
Filterable by date range (defaults to last 7 days).

Usage:
    export GITLAB_TOKEN=<token>
    export JIRA_EMAIL=<email>
    export JIRA_TOKEN=<token>

    python scripts/ymir_cve_metrics.py
    python scripts/ymir_cve_metrics.py --from 2026-04-01 --to 2026-04-28
"""

import argparse
import base64
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import requests

GITLAB_API_URL = "https://gitlab.com/api/v4"
GITLAB_GROUPS = ["redhat/rhel/rpms", "redhat/centos-stream/rpms"]
GITLAB_AUTHOR_DEFAULT = "jotnar-bot"
PER_PAGE = 100

JIRA_URL_DEFAULT = "https://redhat.atlassian.net"
JIRA_LABEL_NOT_AFFECTED = "ymir_triaged_not_affected"


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_jira_auth_headers() -> dict[str, str] | None:
    jira_email = os.getenv("JIRA_EMAIL")
    jira_token = os.getenv("JIRA_TOKEN")
    if not jira_email or not jira_token:
        return None
    credentials = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ─── GitLab ────────────────────────────────────────────────────────────────────


def get_gitlab_session() -> requests.Session:
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        print("Error: GITLAB_TOKEN environment variable is required.", file=sys.stderr)
        sys.exit(1)
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    )
    return session


def _paginate_gitlab_mrs(
    session: requests.Session,
    group: str,
    author: str,
    date_from: datetime,
    date_to: datetime,
    *,
    state: str,
    date_field: str,
) -> list[dict]:
    encoded = quote(group, safe="")
    url = f"{GITLAB_API_URL}/groups/{encoded}/merge_requests"
    params = {
        "state": state,
        "author_username": author,
        f"{date_field}_after": date_from.isoformat(),
        f"{date_field}_before": date_to.isoformat(),
        "per_page": PER_PAGE,
        "order_by": date_field,
        "sort": "asc",
        "include_subgroups": "true",
    }

    result = []
    page = 1
    while True:
        params["page"] = page
        resp = session.get(url, params=params)
        if resp.status_code == 403:
            print(f"  Access denied for '{group}'.", file=sys.stderr)
            break
        resp.raise_for_status()
        mrs = resp.json()
        if not mrs:
            break

        if state == "merged":
            for mr in mrs:
                merged_at = mr.get("merged_at")
                if not merged_at:
                    continue
                dt = _parse_iso(merged_at)
                if date_from <= dt <= date_to:
                    result.append(mr)
        else:
            result.extend(mrs)

        print(f"  Page {page} — {len(result)} MRs so far...", end="\r")
        next_page = resp.headers.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)

    print()
    return result


def _fetch_mrs(
    session: requests.Session,
    groups: list[str],
    authors: list[str],
    date_from: datetime,
    date_to: datetime,
    *,
    state: str,
    date_field: str,
) -> list[dict]:
    all_mrs = []
    for group in groups:
        for author in authors:
            print(f"  Querying {state} MRs in {group} by {author}...")
            mrs = _paginate_gitlab_mrs(
                session,
                group,
                author,
                date_from,
                date_to,
                state=state,
                date_field=date_field,
            )
            all_mrs.extend(mrs)
    return all_mrs


def extract_resolved_jiras(session: requests.Session, mrs: list[dict]) -> tuple[set[str], int]:
    jiras: set[str] = set()
    unmatched = 0
    for mr in mrs:
        project_id = mr["project_id"]
        iid = mr["iid"]
        found: set[str] = set()
        resp = session.get(
            f"{GITLAB_API_URL}/projects/{project_id}/merge_requests/{iid}/commits",
            params={"per_page": 1, "order": "asc"},
        )
        if resp.ok:
            commits = resp.json()
            if commits:
                message = commits[0].get("message", "")
                for line in message.split("\n"):
                    if "resolves:" in line.lower():
                        found.update(re.findall(r"RHEL-\d+", line))
        if found:
            jiras.update(found)
        else:
            unmatched += 1
    return jiras, unmatched


# ─── Jira ──────────────────────────────────────────────────────────────────────


def jira_search(
    jira_url: str,
    headers: dict[str, str],
    jql: str,
    fields: list[str] | None = None,
) -> list[dict]:
    url = f"{jira_url.rstrip('/')}/rest/api/3/search/jql"
    if fields is None:
        fields = ["key"]

    issues: list[dict] = []
    next_page_token = None
    while True:
        payload: dict = {"jql": jql, "maxResults": 500, "fields": fields}
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        resp = requests.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
    return issues


def fetch_triage_closures(
    jira_url: str,
    headers: dict[str, str],
    date_from: datetime,
    date_to: datetime,
) -> list[dict]:
    from_str = date_from.strftime("%Y-%m-%d")
    to_str = date_to.strftime("%Y-%m-%d")
    jql = (
        f"filter = 94376 "
        f"AND labels in ({JIRA_LABEL_NOT_AFFECTED}) "
        f'AND status = Closed AND resolution != "Done-Errata" '
        f'AND resolved >= "{from_str}" AND resolved <= "{to_str}"'
    )
    print(f"  JQL: {jql}")
    return jira_search(jira_url, headers, jql, fields=["key", "resolutiondate"])


def _get_label_added_date(
    jira_url: str,
    headers: dict[str, str],
    issue_key: str,
    label: str,
) -> datetime | None:
    url = f"{jira_url.rstrip('/')}/rest/api/3/issue/{issue_key}/changelog"
    start_at = 0
    while True:
        resp = requests.get(url, headers=headers, params={"startAt": start_at, "maxResults": 100})
        resp.raise_for_status()
        data = resp.json()
        for history in data.get("values", []):
            for item in history.get("items", []):
                if item.get("field") == "labels" and label in (item.get("toString") or "").split():
                    return _parse_iso(history["created"])
        if data.get("isLast", True):
            break
        start_at += len(data.get("values", []))
    return None


def fetch_active_triage(
    jira_url: str,
    headers: dict[str, str],
    date_from: datetime,
    date_to: datetime,
) -> list[dict]:
    jql = f"filter = 94376 AND labels in ({JIRA_LABEL_NOT_AFFECTED}) AND status != Closed"
    print(f"  JQL: {jql}")
    issues = jira_search(jira_url, headers, jql)
    print(f"  Found {len(issues)} not-affected/not-closed, checking label dates...")

    result = []
    for i, issue in enumerate(issues):
        if i > 0:
            time.sleep(0.2)
        print(f"  Checking changelog {i + 1}/{len(issues)}...", end="\r")
        label_date = _get_label_added_date(jira_url, headers, issue["key"], JIRA_LABEL_NOT_AFFECTED)
        if label_date and date_from <= label_date <= date_to:
            result.append(issue)
    print()
    return result


# ─── Reporting ─────────────────────────────────────────────────────────────────


def print_summary(
    date_from: datetime,
    date_to: datetime,
    merged_mrs: int,
    jiras_from_mrs: int,
    triage_closures: int,
    total_solved: int,
    non_merged_mrs: int,
    active_triage: int,
) -> None:
    from_str = date_from.strftime("%Y-%m-%d")
    to_str = date_to.strftime("%Y-%m-%d")

    print(f"\nYmir CVE Activity Report: {from_str} → {to_str}")
    print("─" * 50)

    print("Solved:")
    print(f"  {'MRs merged:':<38} {merged_mrs:>6}")
    print(f"  {'Jiras resolved (from MRs):':<38} {jiras_from_mrs:>6}")
    print(f"  {'Not-affected (closed):':<38} {triage_closures:>6}")
    print(f"  {'Total Jiras solved:':<38} {total_solved:>6}")

    print()
    print("Active:")
    print(f"  {'MRs opened (not yet merged):':<38} {non_merged_mrs:>6}")
    print(f"  {'Not-affected (pending closure):':<38} {active_triage:>6}")


# ─── CLI ───────────────────────────────────────────────────────────────────────


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def parse_date_end(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ymir CVE activity — measure Jira resolution effectiveness",
        epilog=(
            "Required environment variables:\n"
            "  GITLAB_TOKEN   GitLab API token (read_api scope)\n"
            "  JIRA_EMAIL     Jira account email (use your personal account,\n"
            "                 not Ymir's — the saved filter requires personal access)\n"
            "  JIRA_TOKEN     Jira API token for the above account\n"
            "\n"
            "Optional environment variables:\n"
            "  JIRA_URL       Jira base URL (default: https://redhat.atlassian.net)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date (YYYY-MM-DD, default: 7 days ago)",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date inclusive (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--gitlab-author",
        action="append",
        default=None,
        help=f"GitLab username to filter MRs (default: {GITLAB_AUTHOR_DEFAULT}). "
        "Can be specified multiple times for multiple authors.",
    )
    args = parser.parse_args()

    if args.gitlab_author is None:
        args.gitlab_author = [GITLAB_AUTHOR_DEFAULT]

    today = datetime.now(UTC)
    date_to = parse_date_end(args.date_to) if args.date_to else today
    date_from = parse_date(args.date_from) if args.date_from else (today - timedelta(days=7))
    jira_url = os.getenv("JIRA_URL", JIRA_URL_DEFAULT)

    # ── GitLab ──
    session = get_gitlab_session()

    print("Fetching merged MRs from GitLab...")
    # GitLab doesn't support filtering by merged_at directly, so we use updated_at
    # as a proxy and then filter by merged_at in the pagination loop.
    merged_mrs = _fetch_mrs(
        session,
        GITLAB_GROUPS,
        args.gitlab_author,
        date_from,
        date_to,
        state="merged",
        date_field="updated_at",
    )
    print("Extracting resolved Jiras from first commits...")
    resolved_jiras, unmatched_mrs = extract_resolved_jiras(session, merged_mrs)

    print("Fetching opened MRs from GitLab...")
    opened_mrs = _fetch_mrs(
        session,
        GITLAB_GROUPS,
        args.gitlab_author,
        date_from,
        date_to,
        state="opened",
        date_field="created_at",
    )

    triage_closures: list[dict] = []
    active_triage: list[dict] = []

    # ── Jira ──
    headers = get_jira_auth_headers()
    if not headers:
        print(
            "Warning: JIRA_EMAIL or JIRA_TOKEN not set, skipping Jira queries.",
            file=sys.stderr,
        )
    else:
        print("Fetching triage closures from Jira...")
        triage_closures = fetch_triage_closures(jira_url, headers, date_from, date_to)
        print(f"  Found {len(triage_closures)} triage closures.")

        print("Fetching active triage (not-affected, not closed)...")
        active_triage = fetch_active_triage(jira_url, headers, date_from, date_to)
        print(f"  Found {len(active_triage)} active triage Jiras.")

    # ── Deduplicate & report ──
    jiras_from_mrs = len(resolved_jiras) + unmatched_mrs
    triage_keys = {issue["key"] for issue in triage_closures}
    total_solved = len(resolved_jiras | triage_keys) + unmatched_mrs

    print_summary(
        date_from,
        date_to,
        merged_mrs=len(merged_mrs),
        jiras_from_mrs=jiras_from_mrs,
        triage_closures=len(triage_closures),
        total_solved=total_solved,
        non_merged_mrs=len(opened_mrs),
        active_triage=len(active_triage),
    )


if __name__ == "__main__":
    main()
