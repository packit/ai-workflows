#!/usr/bin/env python3
"""Create public repos with a README in a GitLab group.

Usage:
    python3 create_rules_repos.py --input repos.json --group-id 12345
    python3 create_rules_repos.py --name ipxe --name madan-fonts --group-id 12345
    python3 create_rules_repos.py --input repos.json --group-id 12345 --offset 100 --limit 500

The --input file is a JSON list of project names, e.g. ["389-ds-base", "kernel", "glibc"].
Set GITLAB_TOKEN env var or pass --gitlab-token.
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

GITLAB_API = "https://gitlab.com/api/v4"
CS_RULES_GROUP_ID = 131298321

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def api_call(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    while True:
        resp = session.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        retry_after = int(resp.headers.get("Retry-After", 60))
        log.warning("\nRate limited, sleeping %ds", retry_after)
        time.sleep(retry_after)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        help="JSON file containing a list of project names",
    )
    parser.add_argument(
        "--name",
        action="append",
        help="Single project name to create (can be repeated)",
    )
    parser.add_argument(
        "--group-id",
        type=int,
        default=CS_RULES_GROUP_ID,
        help=f"GitLab group ID for the target namespace (default: {CS_RULES_GROUP_ID}, centos-stream/rules)",
    )
    parser.add_argument(
        "--gitlab-token",
        default=os.environ.get("GITLAB_TOKEN"),
        help="GitLab personal access token (default: $GITLAB_TOKEN env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without making API calls",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip the first N components",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only create N repos (0 = all)",
    )
    args = parser.parse_args()

    if not args.gitlab_token and not args.dry_run:
        sys.exit("GITLAB_TOKEN env var or --gitlab-token is required")

    if args.name:
        components = args.name
    elif args.input:
        with open(args.input) as f:
            components = json.load(f)
    else:
        sys.exit("Provide --input or --name")
    if args.offset:
        components = components[args.offset :]
    if args.limit:
        components = components[: args.limit]
    log.info("Components to create: %d", len(components))

    if args.dry_run:
        for c in components:
            print(f"  [dry-run] would create: {c}")
        print(f"\nTotal: {len(components)} repos")
        return

    session = requests.Session()
    session.headers["PRIVATE-TOKEN"] = args.gitlab_token

    created = 0
    skipped = 0
    errors = []
    total = len(components)

    for i, comp in enumerate(components, 1):
        print(
            f"\r[{i}/{total}] Creating {comp:<40} (created={created}, skipped={skipped}, err={len(errors)}) ",
            end="",
            flush=True,
        )

        resp = api_call(
            session,
            "POST",
            f"{GITLAB_API}/projects",
            json={
                "name": comp,
                "path": comp,
                "namespace_id": args.group_id,
                "visibility": "public",
            },
        )

        if resp.status_code == 201:
            project_id = resp.json()["id"]
            readme = (
                f"# {comp}\n\n"
                "Package-specific rules and instructions "
                "to be used mainly by AI agents (Ymir and others).\n\n"
                "Use `AGENTS.md` to define package-specific rules. "
                "These may cover fix strategies (e.g. rebase vs backport), "
                "upstream repo and branch pointers, specfile conventions, "
                "backporting guidance, and other package context.\n"
            )
            commit_resp = api_call(
                session,
                "POST",
                f"{GITLAB_API}/projects/{project_id}/repository/commits",
                json={
                    "branch": "main",
                    "commit_message": "Initial commit",
                    "actions": [{"action": "create", "file_path": "README.md", "content": readme}],
                },
            )
            created += 1
            if commit_resp.status_code != 201:
                log.error("\nProject %s created but README commit failed: %s", comp, commit_resp.status_code)
        elif resp.status_code == 400 and "has already been taken" in resp.text:
            skipped += 1
        else:
            log.error("\nFailed to create %s: %s %s", comp, resp.status_code, resp.text)
            errors.append({"component": comp, "status": resp.status_code, "error": resp.text})

    print()

    print("\n=== Summary ===")
    print(f"  created: {created}")
    print(f"  skipped (already exist): {skipped}")
    print(f"  errors: {len(errors)}")

    if errors:
        with open("create_rules_errors.json", "w") as f:
            json.dump(errors, f, indent=2)
        log.info("Errors saved to create_rules_errors.json")


if __name__ == "__main__":
    main()
