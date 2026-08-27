"""Deterministic helpers for inheriting shipped Z-stream CVE fixes."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel
from specfile import Specfile
from specfile.utils import EVR

from ymir.common.base_utils import check_subprocess, run_subprocess
from ymir.common.constants import BREWHUB_URL
from ymir.common.models import ShippedZStreamCandidate
from ymir.common.utils import _get_koji_build, parse_koji_build_source
from ymir.common.version_utils import parse_rhel_version

_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b", re.IGNORECASE)
_RESOLVES_RE = re.compile(r"^Resolves:\s*(?P<value>.+)$", re.IGNORECASE | re.MULTILINE)


class InheritCandidateError(RuntimeError):
    """A shipped candidate cannot be used by the deterministic fast path."""


class AlreadyInheritedError(InheritCandidateError):
    """The selected Z-stream fix is already contained in Y-stream history."""


class BrewSource(BaseModel):
    """Validated provenance for the dist-git commit used by a Brew build."""

    nvr: str
    repository_url: str
    commit_sha: str
    epoch: int
    version: str

    @property
    def ev(self) -> EVR:
        return EVR(epoch=self.epoch, version=self.version)


async def resolve_brew_source(nvr: str, package: str) -> BrewSource:
    """Resolve and validate the dist-git source recorded by a Brew build."""
    build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, nvr)
    if not build:
        raise InheritCandidateError(f"Brew build not found: {nvr}")
    if build.get("name") and build["name"] != package:
        raise InheritCandidateError(f"Brew build {nvr} belongs to {build['name']}, expected {package}")

    try:
        source, commit_sha = parse_koji_build_source(build)
    except ValueError as exc:
        raise InheritCandidateError(f"Brew build {nvr} has no supported git source") from exc
    if not source.startswith("git+"):
        raise InheritCandidateError(f"Brew build {nvr} has no supported git source")
    repository_url = source.removeprefix("git+")
    if not _FULL_SHA_RE.fullmatch(commit_sha):
        raise InheritCandidateError(f"Brew build {nvr} has an invalid source commit")

    parsed_url = urlparse(repository_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "gitlab.com":
        raise InheritCandidateError(f"Brew build {nvr} has an unsupported source repository")
    expected_suffix = f"/redhat/rhel/rpms/{package}"
    if parsed_url.path.rstrip("/") != expected_suffix:
        raise InheritCandidateError(f"Brew build {nvr} source does not match redhat/rhel/rpms/{package}")

    version = build.get("version")
    if not isinstance(version, str) or not version:
        raise InheritCandidateError(f"Brew build {nvr} has no version")
    try:
        epoch = int(build.get("epoch") or 0)
    except (TypeError, ValueError) as exc:
        raise InheritCandidateError(f"Brew build {nvr} has an invalid epoch") from exc

    return BrewSource(
        nvr=nvr,
        repository_url=repository_url,
        commit_sha=commit_sha.lower(),
        epoch=epoch,
        version=version,
    )


def same_major_candidate(
    candidates: list[ShippedZStreamCandidate],
    y_fix_version: str | None,
) -> ShippedZStreamCandidate | None:
    """Return the single leading Z-stream source for the Y-stream major."""
    parsed_y = parse_rhel_version(y_fix_version or "")
    if not parsed_y:
        return None
    y_major = parsed_y[0]

    matches: list[ShippedZStreamCandidate] = []
    for candidate in candidates:
        matching_versions = [
            parsed
            for version in candidate.fix_versions
            if (parsed := parse_rhel_version(version)) and parsed[0] == y_major
        ]
        if not matching_versions:
            continue
        matches.append(candidate)

    return matches[0] if len(matches) == 1 else None


def spec_matches_brew_version(spec_path: Path, source: BrewSource) -> bool:
    """Return whether cXs and Brew use the same Epoch:Version source base."""
    with Specfile(spec_path) as spec:
        epoch = int(spec.expanded_epoch or 0)
        version = spec.expanded_version
    return EVR(epoch=epoch, version=version) == source.ev


def resolves_keys(commit_message: str) -> set[str]:
    """Extract normalized Jira keys from Resolves footer lines."""
    keys: set[str] = set()
    for match in _RESOLVES_RE.finditer(commit_message):
        keys.update(key.upper() for key in _JIRA_KEY_RE.findall(match.group("value")))
    return keys


async def find_zstream_fix_commit(
    clone_path: Path,
    y_head: str,
    z_build_commit: str,
    z_issue_key: str,
) -> str:
    """Find the one single-issue commit for a shipped Z-stream Jira clone."""
    merge_exit, merge_base, merge_error = await run_subprocess(
        ["git", "merge-base", y_head, z_build_commit],
        cwd=clone_path,
    )
    if merge_exit != 0 or not merge_base.strip():
        raise InheritCandidateError(f"Cannot find a common dist-git base: {merge_error.strip()}")

    _, commits_output = await check_subprocess(
        ["git", "rev-list", "--reverse", f"{merge_base.strip()}..{z_build_commit}"],
        cwd=clone_path,
    )
    commits = [commit for commit in commits_output.splitlines() if commit]

    target_key = z_issue_key.upper()
    matches: list[str] = []
    for commit in commits:
        _, message = await check_subprocess(
            ["git", "log", "-1", "--format=%B", commit],
            cwd=clone_path,
        )
        keys = resolves_keys(message)
        if target_key not in keys:
            continue
        if keys != {target_key}:
            raise InheritCandidateError(
                f"Commit {commit} resolves other Jira issues in addition to {target_key}"
            )
        matches.append(commit)

    if not matches:
        _, all_commits_output = await check_subprocess(
            ["git", "rev-list", "--reverse", z_build_commit],
            cwd=clone_path,
        )
        for commit in all_commits_output.splitlines():
            if not commit or commit in commits:
                continue
            _, message = await check_subprocess(
                ["git", "log", "-1", "--format=%B", commit],
                cwd=clone_path,
            )
            keys = resolves_keys(message)
            if target_key not in keys:
                continue
            if keys != {target_key}:
                raise InheritCandidateError(
                    f"Commit {commit} resolves other Jira issues in addition to {target_key}"
                )
            ancestor_exit, _, _ = await run_subprocess(
                ["git", "merge-base", "--is-ancestor", commit, y_head],
                cwd=clone_path,
            )
            if ancestor_exit == 0:
                raise AlreadyInheritedError(f"{target_key} fix {commit} is already in Y-stream")

    if len(matches) != 1:
        raise InheritCandidateError(f"Expected one commit resolving {target_key}, found {len(matches)}")

    ancestor_exit, _, ancestor_error = await run_subprocess(
        ["git", "merge-base", "--is-ancestor", matches[0], y_head],
        cwd=clone_path,
    )
    if ancestor_exit == 0:
        raise AlreadyInheritedError(f"{target_key} fix {matches[0]} is already in Y-stream")
    if ancestor_exit != 1:
        raise InheritCandidateError(
            f"Cannot check whether {matches[0]} is already inherited: {ancestor_error.strip()}"
        )

    return matches[0]
