"""Collect Amalgamator's inputs from what Ymir already has.

Amalgamator needs six things for one backport: an upstream git repository, the
old commit to port onto, the upstream fix, a short description, an issue label,
and optional build/test commands. Ymir already holds almost all of that:

* the spec in the dist-git clone gives the package version, which names the
  upstream tag to port onto;
* the upstream patch URL gives the upstream repository and, for commit URLs,
  the fix commit itself;
* the mock (or real) Jira data gives the ticket summary used as description;
* ``prepare_normal_backport`` has already written the downloaded upstream patch
  next to the spec.

Everything here is a pure function over paths or URLs so it can be tested
offline against temporary repositories.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

from ymir.common.upstream_tags import find_base_commit_tag

logger = logging.getLogger(__name__)

USER_AGENT = "redhat-ymir-agent"

_PATCH_SUFFIX_RE = re.compile(r"\.(patch|diff)$", re.IGNORECASE)
_COMMIT_RE = re.compile(r"(?:commits?|c)/([a-f0-9]{7,40})", re.IGNORECASE)
_QUERY_COMMIT_RE = re.compile(r"(?:^|[;&])(?:id|h)=([a-f0-9]{7,40})", re.IGNORECASE)
_MACRO_DEF_RE = re.compile(r"^%(?:global|define)\s+(\w+)\s+(.+?)\s*$", re.MULTILINE)
_MACRO_REF_RE = re.compile(r"%\{(\w+)\}|%(\w+)")
_SED_VERSION_RE = re.compile(r"%\(\s*echo\s+%\{(\w+)\}\s*\|\s*sed\s+'s/_/\./g'\s*\)")
_PR_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)$")
_MR_PATH_RE = re.compile(r"^/(.+?)/-/merge_requests/(\d+)$")
_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{4,39}$", re.IGNORECASE)


def _literal_version(text: str) -> str | None:
    """Return the ``Version:`` value from spec text, expanding simple macros.

    Most specs spell the version out. Two conventions need more: the value can
    be an indirection through ``%global`` (``Version: %{foo}``), and libexpat's
    older specs derive it with ``%(echo %{unversion} | sed 's/_/./g')``.
    Anything still holding a macro is reported as unresolved rather than
    returned as a bogus version.
    """
    match = re.search(r"^Version:\s*(.+?)\s*(?:#.*)?$", text, re.MULTILINE)
    if match is None:
        return None
    value = match.group(1).strip()
    if not value.startswith("%"):
        return value

    macros = {name: re.sub(r"\s+#.*$", "", value).strip() for name, value in _MACRO_DEF_RE.findall(text)}
    sed_match = _SED_VERSION_RE.fullmatch(value)
    if sed_match:
        raw = macros.get(sed_match.group(1))
        return raw.replace("_", ".") if raw else None

    for _ in range(5):
        expanded = _MACRO_REF_RE.sub(
            lambda ref: macros.get(ref.group(1) or ref.group(2), ref.group(0)), value
        )
        if expanded == value:
            break
        value = expanded
    if not value or "%" in value or any(char.isspace() for char in value):
        return None
    return value


def read_spec_version(spec_path: Path | str) -> str | None:
    """Return the ``Version:`` value from a spec file.

    ``specfile`` expands macros, which is what we want for tag matching, but it
    needs a full RPM environment. Fall back to the literal line so this helper
    also works in a bare checkout.
    """
    try:
        from specfile import Specfile

        with Specfile(str(spec_path)) as spec:
            version = spec.version
        # `specfile`/rpm leaves shell macros unexpanded in some specs (libexpat
        # spells it `Version: %(echo %{unversion} | sed 's/_/./g')`). That
        # string is not a version, so fall through to the literal parser below,
        # which expands the forms it knows and reports the rest as unresolved.
        if version and "%" not in version:
            return version
    except Exception:
        logger.debug("specfile could not read %s; using the literal Version line", spec_path)

    try:
        text = Path(spec_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _literal_version(text)


def fix_commit_from_patch_url(url: str) -> str | None:
    """Return the commit hash a patch URL points at, if it names one.

    Handles GitHub/GitLab ``/commit/<sha>`` (with or without ``.patch``) and
    gitweb/cgit ``?h=<sha>``/``?id=<sha>`` query forms. Pull request and CI
    artifact URLs have no single commit and return ``None``.
    """
    parsed = urlparse(url)
    match = _COMMIT_RE.search(parsed.path)
    if match:
        return match.group(1)
    # gitweb/cgit put the hash in the query and separate parameters with ';'
    # as well as '&', which parse_qs does not treat as a delimiter.
    query_match = _QUERY_COMMIT_RE.search(parsed.query)
    return query_match.group(1) if query_match else None


def _fetch_json(url: str, timeout: int = 30) -> dict:
    """Fetch and decode a JSON document; empty mapping when it cannot be read."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    token = os.getenv("GITHUB_TOKEN") if "api.github.com" in url else os.getenv("GITLAB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https forge APIs
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("could not read %s", url)
        return {}


def merge_commit_from_pr_url(url: str, timeout: int = 30) -> str | None:
    """Return the merge commit a pull/merge-request URL points at.

    A PR ``.patch`` endpoint returns one message per commit, and Amalgamator
    rejects multi-message mboxes. The merge commit carries the whole topic
    branch relative to its first parent, which Amalgamator cherry-picks with
    ``-m 1``, so it is the handle to use for any multi-commit fix. Only a
    merged request has one; an open or closed request returns ``None`` so the
    caller falls back to the patch.
    """
    parsed = urlparse(url)
    path = _PATCH_SUFFIX_RE.sub("", parsed.path).rstrip("/")

    github = _PR_PATH_RE.match(path)
    if github and parsed.netloc.lower() in {"github.com", "www.github.com"}:
        owner, repo, number = github.groups()
        data = _fetch_json(f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}", timeout)
        return (data.get("merge_commit_sha") or None) if data.get("merged_at") else None

    gitlab = _MR_PATH_RE.match(path)
    if gitlab:
        project, number = gitlab.groups()
        api = f"{parsed.scheme}://{parsed.netloc}/api/v4/projects/{quote(project, safe='')}"
        data = _fetch_json(f"{api}/merge_requests/{number}", timeout)
        if data.get("state") != "merged":
            return None
        return data.get("merge_commit_sha") or data.get("squash_commit_sha") or None

    return None


def resolve_fix_commit(patch_url: str, timeout: int = 30) -> str | None:
    """Return the commit to hand Amalgamator, resolving PR URLs when needed."""
    return fix_commit_from_patch_url(patch_url) or merge_commit_from_pr_url(patch_url, timeout=timeout)


def upstream_repo_url(patch_url: str) -> str | None:
    """Return a clone URL for the upstream project behind *patch_url*.

    Supported shapes are the ones Ymir accepts as upstream patches: GitHub and
    GitLab commit, pull-request/merge-request and compare URLs. Everything
    before the endpoint marker is the project path. gitweb query URLs, release
    tarballs and download-site diffs return ``None`` — there is no project path
    to clone from them.
    """
    parsed = urlparse(patch_url)
    if not parsed.netloc:
        return None

    path = _PATCH_SUFFIX_RE.sub("", parsed.path)

    for marker in (
        "/-/commit/",
        "/-/merge_requests/",
        "/-/compare/",
        "/commit/",
        "/commits/",
        "/pull/",
        "/compare/",
    ):
        if marker in path:
            project = path.split(marker, 1)[0].strip("/")
            break
    else:
        return None

    # GitLab projects can live in subgroups; everything before the marker is
    # the project path, not just the first two segments.
    if project.count("/") < 1:
        return None

    repo = project.removesuffix(".git")
    return f"{parsed.scheme}://{parsed.netloc}/{repo}.git"


def _git(repo_path: Path, *args: str, timeout: int = 300) -> tuple[int, str]:
    # Fixed argv, no shell: the subcommand and its arguments come from callers
    # in this module, not from untrusted input. A hung fetch must not hang the
    # caller forever; AMALGAMATOR_TIMEOUT only bounds Amalgamator itself.
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_path), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git %s timed out after %ss in %s", args, timeout, repo_path)
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def commit_exists(repo_path: Path, sha: str) -> bool:
    """Return whether *sha* names a commit object already in *repo_path*."""
    code, _ = _git(repo_path, "cat-file", "-e", f"{sha}^{{commit}}")
    return code == 0


def fetch_commit(repo_path: Path, sha: str) -> bool:
    """Make *sha* available in *repo_path* when the remote can supply it.

    ``git fetch origin <sha>`` accepts a full object name on servers that allow
    arbitrary SHA fetches, but a short name is not a valid refspec. A short SHA
    is resolved by refreshing the remote refs and asking again.
    """
    code, _ = _git(repo_path, "fetch", "--filter=blob:none", "origin", sha)
    if code == 0:
        return True
    if len(sha) < 40 and _SHORT_SHA_RE.match(sha):
        code, _ = _git(repo_path, "fetch", "--filter=blob:none", "origin")
        if code != 0:
            return False
    return commit_exists(repo_path, sha)


def resolve_base_commit(repo_path: Path, version: str, package: str) -> str | None:
    """Return the upstream commit that corresponds to *version*.

    Uses the same tag conventions as the agent's ``find_base_commit`` tool.
    The package name here is the dist-git package, while the agent derives its
    name from the clone directory (``curl-rhel-9.6-upstream``), so the
    package-prefixed patterns can differ; the unprefixed patterns are tried
    first and are identical.
    """
    code, stdout = _git(repo_path, "tag", "-l")
    if code != 0:
        return None
    tags = stdout.splitlines()
    tag = find_base_commit_tag(tags, version, package)
    if not tag:
        return None
    code, sha = _git(repo_path, "rev-parse", "--verify", f"{tag}^{{commit}}")
    return sha if code == 0 else None


def default_branch_ref(repo_path: Path) -> str:
    """Return the remote-tracking ref for the upstream default branch."""
    code, ref = _git(repo_path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if code == 0 and ref.startswith("refs/remotes/"):
        return ref.removeprefix("refs/remotes/")
    for candidate in ("origin/main", "origin/master"):
        code, _ = _git(repo_path, "rev-parse", "--verify", candidate)
        if code == 0:
            return candidate
    return "HEAD"


def clone_upstream(repo_url: str, destination: Path, timeout: int = 3600) -> Path:
    """Clone *repo_url* into *destination* (blobless, like Ymir's own clone)."""
    destination = Path(destination)
    if (destination / ".git").is_dir():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603
        ["git", "clone", "--filter=blob:none", repo_url, str(destination)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git clone {repo_url} failed: {(proc.stderr or '').strip()[:500]}")
    return destination


def download_patch(url: str, destination: Path, timeout: int = 120) -> Path:
    """Download *url* to *destination*.

    Amalgamator's patch mode ingests a single-mailbox patch, so multi-commit
    PR patches are rejected there; callers should prefer ``fix_commit`` when
    the URL names one.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(  # noqa: S310 - https URLs from fixtures/config
        url, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            destination.write_bytes(response.read())
    except OSError as exc:
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc
    return destination


def read_jira_summary(issue_key: str, jira_dir: Path | str | None = None) -> str | None:
    """Return the ticket summary used as Amalgamator's bug description.

    In mock mode the Jira backend keeps one JSON file per issue under
    ``$JIRA_MOCK_FILES``, with no file extension.
    """
    if jira_dir is None:
        jira_dir = os.getenv("JIRA_MOCK_FILES")
    if not jira_dir:
        return None
    path = Path(jira_dir) / issue_key
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    summary = (data.get("fields") or {}).get("summary")
    return summary.strip() if isinstance(summary, str) and summary.strip() else None
