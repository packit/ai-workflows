#!/usr/bin/env python3
"""Launch Claude Code with the triage skill against mocked JIRA data.

Usage:
    export TESTING_JIRAS_DIR=/path/to/testing-jiras
    ./scripts/run-mock-triage.py RHEL-15216

Prerequisites:
    - claude CLI on PATH
    - ymir-privileged-gateway and ymir-unprivileged-gateway on PATH
    - TESTING_JIRAS_DIR pointing to a clone of
      git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git
    - rhel-config.json in the repo root (copied from templates/ if missing)
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import git as gitpython

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UPSTREAM_SEARCH_URL = "http://upstream-search.hosted.upshift.rdu2.redhat.com:80/v1"


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def require_command(name: str, hint: str) -> None:
    if shutil.which(name) is None:
        fail(f"{name} not found on PATH. {hint}")


def list_scenarios(mock_data_dir: Path) -> list[str]:
    if not mock_data_dir.is_dir():
        return []
    return sorted(p.stem for p in mock_data_dir.glob("*.json"))


def load_fixture(fixture_path: Path) -> dict:
    with open(fixture_path) as fh:
        return json.load(fh)


def extract_blocked_urls(fixture: dict) -> str:
    urls = [r["remote_url"] for r in fixture.get("repos", [])]
    return ",".join(urls)


def extract_zstream_override(fixture: dict) -> str:
    override = fixture.get("zstream_override")
    return json.dumps(override) if override else ""


def setup_mock_repos(fixture: dict, base_dir: Path) -> dict[str, str]:
    """Clone repos at pre-fix state and return git env vars for URL rewriting.

    Each bare clone has its branch ref rewound to the pre-fix commit so that
    the agent sees the repository state *before* the fix was applied.

    Args:
        fixture: The loaded fixture data describing repos to mock.
        base_dir: Directory in which bare clones are created.

    Returns:
        A dict with ``GIT_CONFIG_COUNT``/``KEY``/``VALUE`` entries for
        ``insteadOf`` URL rewriting.
    """
    repos = fixture.get("repos", [])
    if not repos:
        return {}

    git_env: dict[str, str] = {}

    for i, repo_info in enumerate(repos):
        package = repo_info["package"]
        local_path = base_dir / f"{package}.git"

        if local_path.exists():
            shutil.rmtree(local_path)

        print(f"  Cloning {repo_info['remote_url']} (bare) -> {local_path}")
        repo = gitpython.Repo.clone_from(repo_info["remote_url"], str(local_path), bare=True)

        branch = repo_info["branch"]
        repo.git.update_ref(f"refs/heads/{branch}", repo_info["pre_fix_ref"])

        keep_ref = f"refs/heads/{branch}"
        for ref in repo.references:
            if ref.path != keep_ref:
                repo.git.update_ref("-d", ref.path)
        repo.git.gc("--prune=now", "-q")

        git_env[f"GIT_CONFIG_KEY_{i}"] = f"url.file://{local_path}.insteadOf"
        git_env[f"GIT_CONFIG_VALUE_{i}"] = repo_info["remote_url"]

    git_env["GIT_CONFIG_COUNT"] = str(len(repos))
    return git_env


def ensure_writable(directory: Path) -> None:
    for root, _dirs, files in os.walk(directory):
        for name in files:
            path = Path(root) / name
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


def build_mcp_config(
    jira_mock_dir: Path,
    mock_data_dir: Path,
    upstream_search_url: str,
    blocked_urls: str,
    mock_zstreams: str,
    git_env: dict[str, str],
    log_dir: Path,
) -> dict:
    privileged_env = {
        "MCP_TRANSPORT": "stdio",
        "MOCK_JIRA": "true",
        "JIRA_MOCK_FILES": str(jira_mock_dir),
        "JIRA_DRY_RUN": "true",
        "JIRA_URL": os.environ.get("JIRA_URL", "https://redhat.atlassian.net"),
        "JIRA_EMAIL": os.environ.get("JIRA_EMAIL", ""),
        "JIRA_TOKEN": os.environ.get("JIRA_TOKEN", ""),
        "GITLAB_TOKEN": os.environ.get("GITLAB_TOKEN", ""),
        "KRB5CCNAME": os.environ.get("KRB5CCNAME", f"FILE:/tmp/krb5cc_{os.getuid()}"),
        "GIT_REPO_BASEPATH": os.environ.get("GIT_REPO_BASEPATH", "/tmp/ymir-git-repos"),
        "MOCK_BLOCKED_URLS": blocked_urls,
        "MOCK_ZSTREAMS": mock_zstreams,
        "DEBUG_FILE": str(log_dir / "ymir-privileged.log"),
        **git_env,
    }

    return {
        "mcpServers": {
            "ymir-privileged": {
                "command": "ymir-privileged-gateway",
                "env": privileged_env,
            },
            "ymir-unprivileged": {
                "command": "ymir-unprivileged-gateway",
                "env": {
                    "MCP_TRANSPORT": "stdio",
                    "UPSTREAM_SEARCH_API_URL": upstream_search_url,
                    "MOCK_REPOS_DIR": str(mock_data_dir),
                    "MOCK_BLOCKED_URLS": blocked_urls,
                    "MOCK_ZSTREAMS": mock_zstreams,
                    "DEBUG_FILE": str(log_dir / "ymir-unprivileged.log"),
                    **git_env,
                },
            },
        }
    }


def main() -> None:
    testing_jiras_dir = os.environ.get("TESTING_JIRAS_DIR", "")
    mock_data_dir = Path(testing_jiras_dir) / "mock_data" if testing_jiras_dir else None

    epilog_lines = ["available test scenarios (from testing-jiras/mock_data/):"]
    if mock_data_dir and mock_data_dir.is_dir():
        epilog_lines.extend(f"  {scenario}" for scenario in list_scenarios(mock_data_dir))
    else:
        epilog_lines.append("  (set TESTING_JIRAS_DIR to list available scenarios)")

    parser = argparse.ArgumentParser(
        description="Launch Claude Code with the triage skill against mocked data.",
        epilog="\n".join(epilog_lines),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("issue_key", metavar="ISSUE_KEY", help="JIRA issue key (e.g. RHEL-15216)")
    args = parser.parse_args()

    issue = args.issue_key

    # -- Validation -----------------------------------------------------------

    require_command("claude", "Install Claude Code first.")
    require_command(
        "ymir-privileged-gateway",
        "Install ymir-tools first (see skills_installation.md).",
    )
    require_command(
        "ymir-unprivileged-gateway",
        "Install ymir-tools first (see skills_installation.md).",
    )

    if not testing_jiras_dir:
        fail("TESTING_JIRAS_DIR is not set. Point it to a clone of testing-jiras.git.")

    testing_jiras_path = Path(testing_jiras_dir)
    if not testing_jiras_path.is_dir():
        fail(f"TESTING_JIRAS_DIR={testing_jiras_dir} does not exist.")

    jira_mock_dir = testing_jiras_path / "jiras"
    mock_data_dir = testing_jiras_path / "mock_data"

    jira_mock_file = jira_mock_dir / issue
    if not jira_mock_file.is_file():
        fail(f"JIRA mock file not found: {jira_mock_file}")

    fixture_file = mock_data_dir / f"{issue}.json"
    if not fixture_file.is_file():
        fail(f"Repo fixture not found: {fixture_file}")

    rhel_config = REPO_ROOT / "rhel-config.json"
    if not rhel_config.is_file():
        template = REPO_ROOT / "templates" / "rhel-config.json"
        if template.is_file():
            print("Copying templates/rhel-config.json to repo root...")
            shutil.copy2(template, rhel_config)
        else:
            fail(f"rhel-config.json not found in {REPO_ROOT} and no template available.")

    # -- Load fixture and extract config --------------------------------------

    fixture = load_fixture(fixture_file)
    blocked_urls = extract_blocked_urls(fixture)
    mock_zstreams = extract_zstream_override(fixture)

    # -- Prepare mock repos (bare clones at pre-fix state) --------------------

    mock_repo_dir = REPO_ROOT / "logs" / "mock-triage" / "mock-repos"
    mock_repo_dir.mkdir(parents=True, exist_ok=True)

    print(f"Setting up mock repos in {mock_repo_dir} ...")
    git_env = setup_mock_repos(fixture, mock_repo_dir)

    # -- Ensure JIRA mock files are writable ----------------------------------

    ensure_writable(jira_mock_dir)

    # -- Generate temporary MCP config ----------------------------------------

    upstream_search_url = os.environ.get("UPSTREAM_SEARCH_API_URL", DEFAULT_UPSTREAM_SEARCH_URL)

    log_dir = REPO_ROOT / "logs" / "mock-triage"
    log_dir.mkdir(parents=True, exist_ok=True)

    mcp_config = build_mcp_config(
        jira_mock_dir, mock_data_dir, upstream_search_url, blocked_urls, mock_zstreams, git_env, log_dir
    )

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="mock-triage-mcp-", suffix=".json")

    with os.fdopen(tmp_fd, "w") as fh:
        json.dump(mcp_config, fh, indent=2)

    # -- Launch Claude Code ---------------------------------------------------

    print("=== Mock Triage Launcher ===")
    print(f"Issue:            {issue}")
    print(f"JIRA mock dir:    {jira_mock_dir}")
    print(f"Repo fixtures:    {mock_data_dir}")
    print(f"Blocked URLs:     {blocked_urls}")
    print(f"Mock zstreams:    {mock_zstreams or '(none)'}")
    print(f"MCP config:       {tmp_path}")
    print(f"Logs:             {log_dir}/")
    print(f"  privileged:     {log_dir / 'ymir-privileged.log'}")
    print(f"  unprivileged:   {log_dir / 'ymir-unprivileged.log'}")
    print("============================")
    print()

    try:
        result = subprocess.run(
            [
                "claude",
                f"Use the triage skill with jira_issue={issue} and dry_run=true",
                "--mcp-config",
                tmp_path,
            ],
            cwd=REPO_ROOT,
        )
        sys.exit(result.returncode)
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    main()
