"""
Shared utilities for setting up mock CentOS Stream repo fixtures.

When ``MOCK_REPOS_DIR`` is set, per-issue JSON configs are loaded from that
directory.  Each bare clone has its branch ref rewound to a pre-fix commit so
that agents cannot "cheat" by finding an already-applied backport.

When ``MOCK_ZSTREAMS`` is set (JSON string), the z-stream override is applied
via the ``current_z_streams_override`` ContextVar.

When ``MOCK_BLOCKED_URLS`` is set (comma-separated or JSON array),
``RunShellCommandTool`` blocks curl/wget invocations targeting those URL
prefixes.

Directory layout expected under ``MOCK_REPOS_DIR``::

    MOCK_REPOS_DIR/
        RHEL-15216.json
        RHEL-112546.json
        ...

Each JSON file contains::

    {
        "zstream_override": {"9": "rhel-9.2.z"},   // optional
        "repos": [
            {
                "package": "libtiff",
                "remote_url": "https://gitlab.com/redhat/centos-stream/rpms/libtiff",
                "pre_fix_ref": "1d8f0e982d...",
                "branch": "c9s"
            }
        ]
    }
"""

import json
import logging
import os
import tempfile
from pathlib import Path

import git

from ymir.common.version_utils import current_z_streams_override

logger = logging.getLogger(__name__)


def load_fixture_config(issue_key: str, fixtures_dir: str | Path) -> dict | None:
    """Load the test fixture config for a given issue key.

    Args:
        issue_key: The Jira issue key (e.g. ``RHEL-15216``).
        fixtures_dir: Directory containing per-issue JSON config files.

    Returns:
        The parsed config dict, or ``None`` when no config file exists
        for the given issue.
    """
    config_path = Path(fixtures_dir) / f"{issue_key}.json"
    if not config_path.exists():
        return None
    with open(config_path) as fh:
        return json.load(fh)


def load_all_fixture_configs(fixtures_dir: str | Path) -> dict[str, dict]:
    """Load every ``<ISSUE_KEY>.json`` fixture config in a directory.

    Args:
        fixtures_dir: Directory containing per-issue JSON config files.

    Returns:
        A dict mapping issue keys to their parsed config dicts.
    """
    configs: dict[str, dict] = {}
    fixtures_path = Path(fixtures_dir)
    for config_file in sorted(fixtures_path.glob("*.json")):
        issue_key = config_file.stem
        with open(config_file) as fh:
            configs[issue_key] = json.load(fh)
    return configs


def setup_mock_repos(repos: list[dict], issue_key: str, base_dir: Path) -> dict[str, str]:
    """Clone repos at pre-fix state and return a ``git_env`` dict.

    Each bare clone has its branch ref rewound to the pre-fix commit.
    The mocked remote URLs are appended to the ``MOCK_BLOCKED_URLS``
    environment variable so that ``RunShellCommandTool`` blocks direct
    curl/wget access to them.

    Args:
        repos: List of repo dicts, each containing ``package``,
            ``remote_url``, ``pre_fix_ref``, and ``branch`` keys.
        issue_key: The Jira issue key used for naming the local clones.
        base_dir: Directory in which bare clones are created.

    Returns:
        A dict with ``GIT_CONFIG_COUNT``/``KEY``/``VALUE`` entries for
        ``insteadOf`` URL rewriting.
    """
    git_env: dict[str, str] = {}

    for i, repo_info in enumerate(repos):
        local_path = base_dir / f"{issue_key}-{repo_info['package']}.git"
        logger.info(
            "Cloning %s (bare) into %s for %s",
            repo_info["remote_url"],
            local_path,
            issue_key,
        )
        repo = git.Repo.clone_from(repo_info["remote_url"], str(local_path), bare=True)

        keep_branch = repo_info["branch"]
        repo.git.update_ref(f"refs/heads/{keep_branch}", repo_info["pre_fix_ref"])

        keep_ref = f"refs/heads/{keep_branch}"
        refs_to_delete = [ref.path for ref in repo.references if ref.path != keep_ref]
        for ref_path in refs_to_delete:
            repo.git.update_ref("-d", ref_path)

        repo.git.gc("--prune=now", "-q")

        git_env[f"GIT_CONFIG_KEY_{i}"] = f"url.file://{local_path}.insteadOf"
        git_env[f"GIT_CONFIG_VALUE_{i}"] = repo_info["remote_url"]

    git_env["GIT_CONFIG_COUNT"] = str(len(repos))

    _register_blocked_urls([r["remote_url"] for r in repos])

    return git_env


def _register_blocked_urls(urls: list[str]) -> None:
    """Append URLs to the ``MOCK_BLOCKED_URLS`` environment variable.

    Existing entries are preserved (comma-separated). Duplicates are
    deduplicated.

    Args:
        urls: Remote URL strings to block.
    """
    existing = os.getenv("MOCK_BLOCKED_URLS", "")
    current = {u.strip() for u in existing.split(",") if u.strip()} if existing else set()
    current.update(urls)
    os.environ["MOCK_BLOCKED_URLS"] = ",".join(sorted(current))


def apply_zstream_override(override: dict[str, str] | None) -> None:
    """Set the ``current_z_streams_override`` ContextVar if non-empty.

    Args:
        override: Mapping of RHEL major version to z-stream target,
            or ``None`` to skip.
    """
    if override:
        current_z_streams_override.set(override)


def apply_zstream_override_from_env() -> None:
    """Read the ``MOCK_ZSTREAMS`` env var (JSON) and apply it."""
    raw = os.getenv("MOCK_ZSTREAMS")
    if raw:
        apply_zstream_override(json.loads(raw))


def setup_mock_repos_from_env(issue_key: str, base_dir: Path | None = None) -> dict[str, str] | None:
    """Load config from ``MOCK_REPOS_DIR`` for an issue and set up repos.

    Also applies the per-issue z-stream override if present in the config.

    Args:
        issue_key: The Jira issue key (e.g. ``RHEL-15216``).
        base_dir: Directory for bare clones. A temporary directory is
            created when ``None``.

    Returns:
        The ``git_env`` dict on success, or ``None`` when mocking is not
        configured or no config exists for the issue.
    """
    mock_dir = os.getenv("MOCK_REPOS_DIR")
    if not mock_dir:
        return None

    config = load_fixture_config(issue_key, mock_dir)
    if config is None:
        return None

    apply_zstream_override(config.get("zstream_override"))

    repos = config.get("repos")
    if not repos:
        return None

    if base_dir is None:
        base_dir = Path(tempfile.mkdtemp(prefix=f"mock_repos_{issue_key}_"))

    return setup_mock_repos(repos, issue_key, base_dir)
