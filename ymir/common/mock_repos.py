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


def get_mock_gitconfig_path() -> Path:
    """Return the path to the shared ``.mock_gitconfig`` file.

    The file lives under ``$GIT_REPO_BASEPATH`` (defaults to
    ``/git-repos``).  Agent containers include this via
    ``git config --global include.path``; the MCP gateway reads it
    via ``GIT_CONFIG_GLOBAL``.
    """
    return Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")) / ".mock_gitconfig"


def write_mock_gitconfig(git_env: dict[str, str]) -> None:
    """Append ``insteadOf`` rewrites from *git_env* to the shared gitconfig.

    Entries from *git_env* (``GIT_CONFIG_COUNT``/``KEY``/``VALUE``) are
    converted to standard gitconfig ``[url "…"] insteadOf = …`` sections
    and **appended** to the file so that multiple callers (one per test
    case / issue) can contribute rewrites concurrently.

    Stale entries pointing to non-existent local paths are pruned first.

    Args:
        git_env: Dict with ``GIT_CONFIG_COUNT``, ``GIT_CONFIG_KEY_<n>``,
            and ``GIT_CONFIG_VALUE_<n>`` entries describing URL rewrites.
    """
    gitconfig_path = get_mock_gitconfig_path()

    # Prune stale entries from previous runs
    if gitconfig_path.exists():
        try:
            with git.GitConfigParser(str(gitconfig_path), read_only=True) as parser:
                stale_sections = []
                for section in parser.sections():
                    if section.startswith('url "file://'):
                        path_str = section[len('url "file://') : -1]
                        if not Path(path_str).exists():
                            stale_sections.append(f"url.file://{path_str}")
            if stale_sections:
                git_cmd = git.Git()
                for section in stale_sections:
                    git_cmd.config("--file", str(gitconfig_path), "--remove-section", section)
        except Exception as e:
            logger.warning("Failed to clean up stale gitconfig entries: %s", e)

    # Append new entries
    git_cmd = git.Git()
    count = int(git_env.get("GIT_CONFIG_COUNT", "0"))
    for i in range(count):
        key = git_env.get(f"GIT_CONFIG_KEY_{i}", "")
        value = git_env.get(f"GIT_CONFIG_VALUE_{i}", "")
        if not key or not value:
            continue
        git_cmd.config("--file", str(gitconfig_path), "--add", key, value)

    logger.info("Wrote insteadOf rewrites to %s", gitconfig_path)


def cleanup_mock_gitconfig() -> None:
    """Remove the shared ``.mock_gitconfig`` file if it exists."""
    get_mock_gitconfig_path().unlink(missing_ok=True)


def setup_mock_repos(repos: list[dict], issue_key: str, base_dir: Path) -> dict[str, str]:
    """Clone repos at pre-fix state and return a ``git_env`` dict.

    Each bare clone has its branch ref rewound to the pre-fix commit.
    The mocked remote URLs are appended to the ``MOCK_BLOCKED_URLS``
    environment variable so that ``RunShellCommandTool`` blocks direct
    curl/wget access to them.

    The ``insteadOf`` rewrites are also written to a shared
    ``.mock_gitconfig`` file under ``$GIT_REPO_BASEPATH`` so that the
    MCP gateway (separate container) and agent git commands (via
    ``include.path``) can transparently redirect to the local clones.

    When multiple entries share the same package (and remote URL), a
    single bare clone is reused — additional branch refs are created
    via ``git fetch`` into the existing repository.  This avoids
    conflicting ``insteadOf`` URL rewrites in Git config.

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
    gitlab_token = os.getenv("GITLAB_TOKEN")

    cloned: dict[str, tuple[Path, git.Repo, list[str]]] = {}
    remote_urls: dict[str, list[str]] = {}

    for repo_info in repos:
        pkg = repo_info["package"]
        clone_url = repo_info["remote_url"]
        if gitlab_token and clone_url.startswith("https://gitlab.com/"):
            clone_url = clone_url.replace("https://", f"https://oauth2:{gitlab_token}@", 1)

        if pkg not in cloned:
            local_path = base_dir / f"{issue_key}-{pkg}.git"
            logger.info(
                "Cloning %s (bare) into %s for %s",
                repo_info["remote_url"],
                local_path,
                issue_key,
            )
            repo = git.Repo.clone_from(clone_url, str(local_path), bare=True)
            cloned[pkg] = (local_path, repo, [])
            remote_urls[pkg] = [repo_info["remote_url"]]
        else:
            local_path, repo, _ = cloned[pkg]
            logger.info(
                "Fetching ref %s into existing bare repo %s for %s",
                repo_info["pre_fix_ref"],
                local_path,
                issue_key,
            )
            repo.git.fetch(clone_url, repo_info["pre_fix_ref"])
            if repo_info["remote_url"] not in remote_urls[pkg]:
                remote_urls[pkg].append(repo_info["remote_url"])

        keep_branch = repo_info["branch"]
        repo.git.update_ref(f"refs/heads/{keep_branch}", repo_info["pre_fix_ref"])
        cloned[pkg][2].append(f"refs/heads/{keep_branch}")

    idx = 0
    for pkg, (local_path, repo, keep_refs) in cloned.items():
        refs_to_delete = [ref.path for ref in repo.references if ref.path not in keep_refs]
        for ref_path in refs_to_delete:
            repo.git.update_ref("-d", ref_path)
        repo.git.gc("--prune=now", "-q")

        for url in remote_urls[pkg]:
            git_env[f"GIT_CONFIG_KEY_{idx}"] = f"url.file://{local_path}.insteadOf"
            git_env[f"GIT_CONFIG_VALUE_{idx}"] = url
            idx += 1

    git_env["GIT_CONFIG_COUNT"] = str(idx)

    write_mock_gitconfig(git_env)
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
