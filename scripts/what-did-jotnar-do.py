#!/usr/bin/env python3
"""
A simple script that outputs a brief summary of what Jötnar did so far in the pilot.

It prints:
- A number of all issues assigned to Jötnar
- A number of all issues that were processed
- A number of all merge requests it opened
- A number of MRs that were closed
- A number of MRs that were merged
"""

import argparse
import asyncio
import os
import sys
from urllib.parse import urljoin
from urllib.parse import quote

import aiohttp


def _get_jira_headers(token: str) -> dict[str, str]:
    """Get headers for Jira API requests."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def get_jotnar_issues_basic_count() -> tuple[int, int]:
    """
    Get count of all issues that Jötnar has processed (have any jotnar_* labels).
    Returns a tuple of (total issues, issues with any jotnar_* labels).
    """
    jira_url = os.getenv("JIRA_URL", "https://issues.redhat.com")
    jira_token = os.getenv("JIRA_TOKEN")

    if not jira_token:
        print("Warning: JIRA_URL or JIRA_TOKEN not set, skipping Jira queries", file=sys.stderr)
        return 0, 0

    # Query for all jotnar issues
    jqls = ["project=RHEL AND AssignedTeam = rhel-jotnar", ]
    # Jotnar labels for finished runs
    jotnar_labels = [
        "jotnar_no_action_needed",
        "jotnar_rebased",
        "jotnar_backported",
        "jotnar_rebase_errored",
        "jotnar_backport_errored",
        "jotnar_triage_errored",
        "jotnar_rebase_failed",
        "jotnar_backport_failed",
        "jotnar_needs_attention",
    ]
    # Build a JQL clause for all jotnar_* labels
    jql_labels = ", ".join(jotnar_labels)
    jqls.append(f"project=RHEL AND labels in ({jql_labels})")

    results = []
    async with aiohttp.ClientSession() as session:
        try:
            for jql in jqls:
                json_payload = {
                    "jql": jql,
                    "startAt": 0,
                    "maxResults": 0,  # We only want the count
                    "fields": ["key"]
                }

                url = urljoin(jira_url, "rest/api/2/search")
                async with session.post(
                    url,
                    json=json_payload,
                    headers=_get_jira_headers(jira_token)
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                    results.append(data.get("total", 0))
        except Exception as e:
            print(f"Error querying Jira issues: {e}", file=sys.stderr)
            return 0, 0
    return results[0], results[1]


async def get_gitlab_stats(namespace: str = "redhat/centos-stream/rpms") -> dict[str, int]:
    """Get GitLab statistics for merge requests created by Jötnar."""
    gitlab_token = os.getenv("GITLAB_TOKEN")
    base_url = os.getenv("GITLAB_URL", "https://gitlab.com/api/v4/")

    if not gitlab_token:
        print("Warning: GITLAB_TOKEN not set, skipping GitLab queries", file=sys.stderr)
        return {"mrs_opened": 0, "mrs_closed": 0, "mrs_merged": 0}

    # The project id or namespace must be url-encoded
    encoded_namespace = quote(namespace, safe="")

    headers = {
        "PRIVATE-TOKEN": gitlab_token,
    }

    # Jötnar bot username or id
    jotnar_username = os.getenv("JOTNAR_GITLAB_USERNAME", "jotnar-bot")

    # Helper to count MRs by state
    async def count_mrs(state: str) -> int:
        url = f"{base_url}groups/{encoded_namespace}/merge_requests"
        params = {
            "state": state,
            "author_username": jotnar_username,
            "per_page": 1,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                resp.raise_for_status()
                # The total number is in the X-Total header
                return int(resp.headers.get("X-Total", "0"))

    opened = await count_mrs("opened")
    closed = await count_mrs("closed")
    merged = await count_mrs("merged")

    return {
        "mrs_opened": opened,
        "mrs_closed": closed,
        "mrs_merged": merged,
    }


async def main():
    """Main function to gather and display Jötnar statistics."""
    parser = argparse.ArgumentParser(description="Get Jötnar pilot statistics")
    parser.add_argument("--namespace", default="redhat/centos-stream/rpms",
                       help="GitLab namespace to query for merge requests "
                       "(default: redhat/centos-stream/rpms)")
    parser.add_argument("--jira-only", action="store_true",
                       help="Only query Jira statistics, skip GitLab")
    parser.add_argument("--gitlab-only", action="store_true",
                       help="Only query GitLab statistics, skip Jira")

    args = parser.parse_args()

    print("Jötnar Pilot Statistics")
    print("=" * 50)

    # Check for conflicting flags
    if args.jira_only and args.gitlab_only:
        print("Error: Cannot use both --jira-only and --gitlab-only flags simultaneously")
        sys.exit(1)

    # Get Jira statistics (unless gitlab-only is specified)
    if not args.gitlab_only:
        total_issues, issues_processed = await get_jotnar_issues_basic_count()

        # Display Jira results
        print(f"Issues assigned to Jötnar: {total_issues}")
        print(f"Issues processed: {issues_processed}")
    else:
        print("Jira statistics skipped (--gitlab-only flag)")

    # Get GitLab statistics (unless jira-only is specified)
    if not args.jira_only:
        # Get GitLab statistics
        gitlab_stats = await get_gitlab_stats(args.namespace)

        # Display GitLab results
        print(f"\nGitLab Statistics (namespace: {args.namespace})")
        print(f"Merge requests opened: {gitlab_stats['mrs_opened']}")
        print(f"Merge requests closed: {gitlab_stats['mrs_closed']}")
        print(f"Merge requests merged: {gitlab_stats['mrs_merged']}")
    else:
        print("\nGitLab statistics skipped (--jira-only flag)")


if __name__ == "__main__":
    asyncio.run(main())
