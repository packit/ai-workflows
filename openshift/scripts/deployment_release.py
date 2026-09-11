#!/usr/bin/env python3
"""Run a Ymir deployment.

This module owns the deployment flow: selecting the previous deployment
reference, reviewing upstream source commits and local deployment configuration,
extracting release notes, invoking the OpenShift apply script, and creating the
immutable deployment tag after a successful deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TAG_PREFIX = "deployed/"
GITHUB_REPOSITORY = "packit/ai-workflows"
GITHUB_API_URL = "https://api.github.com"
GITHUB_API_TIMEOUT = 10
RELEASE_NOTES_RE = re.compile(
    r"(?ms)^[ \t]*RELEASE NOTES BEGIN[ \t]*\r?\n"
    r"(?P<notes>.*?)\r?\n^[ \t]*RELEASE NOTES END[ \t]*\r?$"
)
NOT_IMPORTANT_VALUES = {"", "n/a", "none", "none."}
PREPARE_CANCELLED = 2
MAX_TAG_ATTEMPTS = 10


class DeploymentContext(TypedDict):
    """Data handed from the preparation phase to the deployment finalizer."""

    repo: Path
    base_label: str
    source_head: str
    config_head: str
    tag: str
    changelog: str


class ReleaseNotesPullRequest(TypedDict):
    """The PR fields retained for the generated changelog."""

    number: int
    url: str
    notes: str


class ReleaseError(RuntimeError):
    """An error that should abort the deployment before or after OpenShift."""


def run_command(
    command: list[str], *, check: bool = True, stream_output: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a command while retaining useful, bounded error output.

    When ``check`` is true, command failures are converted to ``ReleaseError``
    rather than ``subprocess.CalledProcessError`` so the caller gets a concise,
    bounded error message suitable for the deployment CLI.
    """

    try:
        completed = subprocess.run(  # noqa: S603 - command is assembled internally
            command,
            capture_output=not stream_output,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ReleaseError(f"failed to run {' '.join(command)}: {error}") from error

    if check and completed.returncode != 0:
        detail = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        suffix = f": {detail}" if detail else ""
        raise ReleaseError(
            f"command {' '.join(command)} failed with exit code {completed.returncode}{suffix}"
        )
    return completed


def git(
    repo: Path,
    *arguments: str,
    check: bool = True,
    stream_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["git", "-C", str(repo), *arguments],
        check=check,
        stream_output=stream_output,
    )


def repository_root() -> Path:
    script_directory = Path(__file__).resolve().parent
    result = git(script_directory, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def ensure_clean_worktree(repo: Path) -> None:
    # Deployment files are tracked; untracked local notes or scratch files do
    # not change the commit that is recorded in the deployment tag.
    status = git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if status:
        raise ReleaseError("the Git worktree has tracked changes; commit or stash them before deploying")


def repository_remote(repo: Path, remote: str) -> str:
    remotes = git(repo, "remote").stdout.splitlines()
    if remote not in remotes:
        raise ReleaseError(f"the {remote!r} Git remote is not configured")
    return remote


def fetch_tags(repo: Path, remote: str) -> None:
    print(f"Fetching {remote}/main and deployment tags from {remote}...")
    git(repo, "fetch", "--progress", remote, "main", "--tags", stream_output=True)


def ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def local_tag_exists(repo: Path, tag: str) -> bool:
    return ref_exists(repo, f"refs/tags/{tag}")


def remote_tag_exists(repo: Path, remote: str, tag: str) -> bool:
    result = git(
        repo,
        "ls-remote",
        "--exit-code",
        remote,
        f"refs/tags/{tag}",
        check=False,
    )
    if result.returncode not in (0, 2):
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleaseError(f"could not check whether remote tag {tag!r} exists: {detail}")
    return result.returncode == 0


def remote_deployment_tags(repo: Path, remote: str) -> set[str]:
    result = git(repo, "ls-remote", "--tags", "--refs", remote, f"refs/tags/{TAG_PREFIX}*")
    tags: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            continue
        ref = fields[1]
        if ref.startswith("refs/tags/"):
            tags.add(ref.removeprefix("refs/tags/"))
    return tags


def resolve_commit(repo: Path, ref: str) -> str:
    result = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return result.stdout.strip()


def compare_main_revisions(
    repo: Path,
    remote: str,
    config_head: str,
    source_head: str,
    branch: str,
) -> tuple[int, int]:
    if branch != "main":
        branch_name = branch or "detached HEAD"
        print(
            f"Warning: deployment configuration is from {branch_name}; "
            f"the image/source revision will still be {remote}/main.",
            file=sys.stderr,
        )

    counts = git(
        repo,
        "rev-list",
        "--left-right",
        "--count",
        f"{config_head}...{source_head}",
    ).stdout.split()
    if len(counts) != 2:
        raise ReleaseError(f"could not compare local deployment configuration with {remote}/main")
    local_ahead, remote_ahead = int(counts[0]), int(counts[1])
    return local_ahead, remote_ahead


def latest_deployment_tag(repo: Path, remote: str) -> str | None:
    remote_tags = remote_deployment_tags(repo, remote)
    if not remote_tags:
        return None

    result = git(
        repo,
        "for-each-ref",
        "--sort=-creatordate",
        "--format=%(refname:short)",
        f"refs/tags/{TAG_PREFIX}",
    )
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for tag in tags:
        if tag in remote_tags:
            return tag
    return None


def deployment_base(repo: Path, remote: str) -> tuple[str, str]:
    tag = latest_deployment_tag(repo, remote)
    if not tag:
        raise ReleaseError(
            f"no deployed/* tag exists on remote {remote!r}; create and push an initial "
            "deployed/<timestamp> tag at the last deployed source revision before deploying"
        )
    return tag, resolve_commit(repo, tag)


def ensure_ancestor(repo: Path, base: str, source_head: str) -> None:
    result = git(repo, "merge-base", "--is-ancestor", base, source_head, check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"the previous deployment ({base[:12]}) is not an ancestor of "
            f"candidate source revision ({source_head[:12]}); refusing to guess "
            "the changelog range"
        )


def proposed_tag_name(repo: Path, remote: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{TAG_PREFIX}{timestamp}"
    for attempt in range(MAX_TAG_ATTEMPTS):
        # The unsuffixed name is the first candidate; -2 denotes the second.
        suffix = "" if attempt == 0 else f"-{attempt + 1}"
        candidate = f"{base_name}{suffix}"
        if not local_tag_exists(repo, candidate) and not remote_tag_exists(repo, remote, candidate):
            return candidate
    raise ReleaseError(f"could not find an unused deployment tag after {MAX_TAG_ATTEMPTS} attempts")


def candidate_log(repo: Path, base: str, source_head: str) -> str:
    result = git(
        repo,
        "--no-pager",
        "log",
        "--oneline",
        "--decorate",
        "--graph",
        f"{base}..{source_head}",
    )
    return result.stdout.rstrip()


def extract_release_notes(description: str | None) -> str | None:
    if not description:
        return None
    match = RELEASE_NOTES_RE.search(description)
    if not match:
        return None
    notes = match.group("notes").strip()
    if notes.casefold() in NOT_IMPORTANT_VALUES:
        return None
    return notes


def github_api_get(path: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "packit-ai-workflows-deployment",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(  # noqa: S310 - fixed GitHub API URL
        f"{GITHUB_API_URL}{path}", headers=headers, method="GET"
    )
    try:
        # The API URL and path are internal values, not user-provided URLs.
        with urlopen(request, timeout=GITHUB_API_TIMEOUT) as response:  # noqa: S310
            status_code = response.status
            response_headers = response.headers
            body = response.read()
    except HTTPError as error:
        status_code = error.code
        response_headers = error.headers or {}
        body = error.read()
    except URLError as error:
        raise ReleaseError(f"GitHub API request {path} failed: {error}") from error

    if status_code >= 400:
        detail = body.decode("utf-8", errors="replace").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        if status_code == 401 and token:
            detail = "invalid GITHUB_TOKEN; unset it for anonymous access or provide a valid token"
        elif status_code in {403, 429} and response_headers.get("X-RateLimit-Remaining") == "0":
            detail = "GitHub API rate limit exceeded"
            if not token:
                detail += "; set GITHUB_TOKEN to use authenticated requests"
        suffix = f": {detail}" if detail else ""
        raise ReleaseError(f"GitHub API request {path} failed with HTTP {status_code}{suffix}")

    try:
        return json.loads(body)
    except ValueError as error:
        raise ReleaseError(f"GitHub API returned invalid JSON for {path}") from error


def associated_pull_requests(repository: str, commit: str) -> list[dict[str, Any]]:
    value = github_api_get(f"/repos/{repository}/commits/{commit}/pulls?per_page=100")
    if not isinstance(value, list):
        raise ReleaseError(f"GitHub API returned unexpected PR data for commit {commit[:12]}")

    pull_requests: list[dict[str, Any]] = []
    for pull_request in value:
        if not isinstance(pull_request, dict):
            raise ReleaseError(f"GitHub API returned unexpected PR data for commit {commit[:12]}")
        number = pull_request.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ReleaseError(
                f"GitHub API returned a non-numeric PR number for commit {commit[:12]}: {number!r}"
            )
        pull_requests.append(pull_request)
    return pull_requests


def collect_release_notes(
    repo: Path, base: str, source_head: str
) -> tuple[list[ReleaseNotesPullRequest], list[int], list[str]]:
    commit_result = git(repo, "rev-list", "--reverse", f"{base}..{source_head}")
    commits = [line.strip() for line in commit_result.stdout.splitlines() if line.strip()]
    repository = GITHUB_REPOSITORY
    prs: list[ReleaseNotesPullRequest] = []
    missing_notes: list[int] = []
    commits_without_prs: list[str] = []
    seen_prs: set[int] = set()

    for commit in commits:
        pull_requests = associated_pull_requests(repository, commit)
        if not pull_requests:
            commits_without_prs.append(commit[:12])
        for pull_request in pull_requests:
            number = pull_request["number"]
            if number in seen_prs:
                continue
            seen_prs.add(number)
            notes = extract_release_notes(pull_request.get("body"))
            if notes is None:
                missing_notes.append(number)
                continue
            url = pull_request.get("html_url")
            if not isinstance(url, str) or not url:
                url = f"https://github.com/{repository}/pull/{number}"
            prs.append(
                {
                    "number": number,
                    "url": url,
                    "notes": notes,
                }
            )
    return prs, missing_notes, commits_without_prs


def format_changelog(base_label: str, prs: list[ReleaseNotesPullRequest], missing_notes: list[int]) -> str:
    lines = [f"Changes since `{base_label}`:"]
    if prs:
        for pr in prs:
            notes_lines = pr["notes"].splitlines()
            first_line = f"- {notes_lines[0]} ([#{pr['number']}]({pr['url']}))"
            lines.append(first_line)
            lines.extend(f"  {line}" for line in notes_lines[1:])
    else:
        lines.append("- No user-facing release notes found.")

    if missing_notes:
        lines.extend(
            [
                "",
                "PRs without a usable release-notes section: "
                + ", ".join(f"#{number}" for number in missing_notes),
            ]
        )
    return "\n".join(lines)


def print_changelog(base_ref: str, head_ref: str | None, remote: str) -> None:
    repo = repository_root()
    remote = repository_remote(repo, remote)
    head_ref = head_ref or f"{remote}/main"
    base = resolve_commit(repo, base_ref)
    head = resolve_commit(repo, head_ref)
    print("Collecting release notes from GitHub...", file=sys.stderr)
    prs, missing_notes, commits_without_prs = collect_release_notes(repo, base, head)
    changelog = format_changelog(base_ref, prs, missing_notes)
    print(changelog)
    if commits_without_prs:
        print(
            "Warning: no associated PR was found for commits: " + ", ".join(commits_without_prs),
            file=sys.stderr,
        )


def ask_for_confirmation() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ReleaseError(
            "deployment confirmation requires an interactive terminal; use --dry-run for testing"
        )
    answer = (
        input("Deploy the shown source revision with the shown deployment configuration? [y/N] ")
        .strip()
        .casefold()
    )
    if answer not in {"y", "yes"}:
        print("Deployment cancelled; no OpenShift changes or Git tag were made.")
        raise SystemExit(PREPARE_CANCELLED)


def prepare(dry_run: bool, remote: str) -> DeploymentContext:
    repo = repository_root()
    ensure_clean_worktree(repo)
    remote = repository_remote(repo, remote)
    config_head = resolve_commit(repo, "HEAD")
    branch = git(repo, "branch", "--show-current").stdout.strip()
    fetch_tags(repo, remote)
    remote_main = f"refs/remotes/{remote}/main"
    if not ref_exists(repo, remote_main):
        raise ReleaseError(f"remote branch {remote}/main is not available after fetching")
    source_head = resolve_commit(repo, remote_main)
    local_ahead, remote_ahead = compare_main_revisions(repo, remote, config_head, source_head, branch)
    print(f"Checking the latest deployed/* tag on {remote}...")
    base_label, base = deployment_base(repo, remote)
    ensure_ancestor(repo, base, source_head)
    tag = proposed_tag_name(repo, remote)
    log = candidate_log(repo, base, source_head)

    print(f"Previous deployment: {base_label}")
    print("\nDeployment revisions:")
    print(f"  Images/source ({remote}/main):       {source_head}")
    print(f"  Deployment config (local HEAD): {config_head}")
    if local_ahead or remote_ahead:
        print(
            f"\nWARNING: local deployment configuration diverges from {remote}/main.",
            file=sys.stderr,
        )
        print(
            f"  Difference: {local_ahead} local commit(s) ahead, {remote_ahead} commit(s) behind.",
            file=sys.stderr,
        )
        print(
            "  OpenShift will apply local manifests/scripts, while images, "
            f"the changelog, and the deployment tag follow {remote}/main.",
            file=sys.stderr,
        )
    else:
        print("  Configuration and image/source revision are aligned.")
    print(f"Proposed tag:        {tag}")
    print(f"\nChanges in {remote}/main since the previous deployment:")
    print(log or "(no commits since the previous deployment)")

    if not dry_run:
        ask_for_confirmation()

    print("\nCollecting release notes from GitHub...")
    prs, missing_notes, commits_without_prs = collect_release_notes(repo, base, source_head)
    changelog = format_changelog(base_label, prs, missing_notes)
    context: DeploymentContext = {
        "repo": repo,
        "base_label": base_label,
        "source_head": source_head,
        "config_head": config_head,
        "tag": tag,
        "changelog": changelog,
    }
    if commits_without_prs:
        print(
            "Warning: no associated PR was found for commits: " + ", ".join(commits_without_prs),
            file=sys.stderr,
        )

    if dry_run:
        print("\nChangelog:")
        print(changelog)
        print("\nDry run complete. No OpenShift changes or Git tags were created.")
    return context


def run_openshift_deployment() -> None:
    script = Path(__file__).resolve().parents[1] / "deploy-oc.sh"
    print("\nApplying OpenShift manifests and importing images...")
    try:
        subprocess.run(  # noqa: S603 - script path is resolved from this repository
            ["/bin/sh", str(script)], check=True
        )
    except OSError as error:
        raise ReleaseError(f"failed to run OpenShift deployment script: {error}") from error
    except subprocess.CalledProcessError as error:
        raise ReleaseError(f"OpenShift deployment failed with exit code {error.returncode}") from error


def finalize(context: DeploymentContext, remote: str) -> None:
    repo = context["repo"]
    source_head = context["source_head"]
    config_head = context["config_head"]
    tag = context["tag"]
    current_config_head = resolve_commit(repo, "HEAD")
    if current_config_head != config_head:
        raise ReleaseError(
            "local deployment configuration changed during deployment: "
            f"expected {config_head[:12]}, found {current_config_head[:12]}"
        )
    ensure_clean_worktree(repo)
    if local_tag_exists(repo, tag) or remote_tag_exists(repo, remote, tag):
        raise ReleaseError(f"deployment tag {tag!r} already exists")

    message = "Automated deployment"
    print(f"\nCreating deployment tag {tag} at {source_head}...")
    git(repo, "tag", "--annotate", "--message", message, tag, source_head)
    try:
        print(f"Pushing deployment tag {tag} to {remote}...")
        git(repo, "push", "--progress", remote, tag, stream_output=True)
    except ReleaseError:
        git(repo, "tag", "--delete", tag, check=False)
        raise

    print(f"\nDeployment succeeded. Tag pushed: {tag}")
    print(f"Images/source revision tagged: {source_head}")
    print(f"Deployment configuration used: {config_head}")
    if source_head != config_head:
        print(
            f"The tag points to {remote}/main; the deployed configuration came "
            "from a different local revision."
        )
    print("\nChangelog:")
    print(context["changelog"])


def deploy(dry_run: bool, remote: str) -> None:
    context = prepare(dry_run, remote)
    if dry_run:
        return
    run_openshift_deployment()
    finalize(context, remote)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remote",
        default="upstream",
        help="Git remote used for the source branch and deployment tag (default: upstream)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    deploy_parser = subparsers.add_parser("deploy", help="review and run a deployment")
    deploy_parser.add_argument("--dry-run", action="store_true")

    changelog_parser = subparsers.add_parser("changelog", help="print release notes between two revisions")
    changelog_parser.add_argument("base", help="starting revision or deployment tag")
    changelog_parser.add_argument(
        "head",
        nargs="?",
        help="ending revision (default: <remote>/main)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "deploy":
            deploy(args.dry_run, args.remote)
        else:
            print_changelog(args.base, args.head, args.remote)
    except SystemExit as error:
        if error.code == PREPARE_CANCELLED:
            return 0
        return int(error.code)
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
