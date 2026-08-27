"""Deterministic helpers for inheriting shipped Z-stream CVE fixes."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from specfile import Specfile
from specfile.prep import AutopatchMacro, AutosetupMacro, PatchMacro
from specfile.utils import EVR

from ymir.common.base_utils import check_subprocess, run_subprocess
from ymir.common.constants import BREWHUB_URL
from ymir.common.models import ShippedZStreamCandidate
from ymir.common.utils import (
    _get_koji_build,
    get_all_patches,
    get_all_sources,
    parse_koji_build_source,
)
from ymir.common.version_utils import parse_rhel_version

_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b", re.IGNORECASE)
_RESOLVES_RE = re.compile(r"^Resolves:\s*(?P<value>.+)$", re.IGNORECASE | re.MULTILINE)
_YMIR_ATTRIBUTION_RE = re.compile(r"^\s*Assisted-by:\s*Ymir\s*$", re.IGNORECASE)


class InheritCandidateError(RuntimeError):
    """A shipped candidate cannot be used by the deterministic fast path."""


class AlreadyInheritedError(InheritCandidateError):
    """The selected Z-stream fix is already contained in Y-stream history."""


class ImmutablePatchError(InheritCandidateError):
    """An inherited patch no longer matches the exact Z-stream Git blob."""


class InheritedPatchApplyError(InheritCandidateError):
    """An immutable inherited patch does not apply cleanly to Y-stream sources."""


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


class CommitFile(BaseModel):
    """A path changed by the selected single-CVE commit."""

    status: str
    path: str


class IntegratedChange(BaseModel):
    """Source context and immutable patches prepared for LLM-guided adaptation."""

    commit_sha: str
    commit_message: str
    changed_files: list[str]
    patch_files: list[str] = Field(default_factory=list)
    patch_blob_ids: dict[str, str] = Field(default_factory=dict)
    source_spec_diff: str = ""
    source_spec_changed: bool = False


@dataclass(frozen=True)
class _SpecSafetySnapshot:
    name: str
    epoch: str
    version: str
    release: tuple[str, str]
    sources: tuple[tuple[int, str], ...]
    changelog: str


@dataclass(frozen=True)
class _PatchApplication:
    strip: int


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
        parsed_versions = [
            parsed
            for version in candidate.fix_versions
            if (parsed := parse_rhel_version(version)) and parsed[2]
        ]
        if len(parsed_versions) != 1 or parsed_versions[0][0] != y_major:
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


async def _commit_messages(
    clone_path: Path,
    revision: str,
    *,
    grep: str | None = None,
) -> list[tuple[str, str]]:
    """Read commit IDs and messages with one git process."""
    command = ["git", "log", "--reverse", "--format=%x1e%H%x1f%B"]
    if grep:
        command.extend(["--regexp-ignore-case", f"--grep={grep}"])
    command.append(revision)
    output, _ = await check_subprocess(command, cwd=clone_path)

    commits: list[tuple[str, str]] = []
    for record in (output or "").split("\x1e"):
        commit, separator, message = record.partition("\x1f")
        if separator and commit.strip():
            commits.append((commit.strip(), message.strip()))
    return commits


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
    merge_base = (merge_base or "").strip()
    if merge_exit != 0 or not merge_base:
        raise InheritCandidateError(f"Cannot find a common dist-git base: {(merge_error or '').strip()}")

    candidate_commits = await _commit_messages(
        clone_path,
        f"{merge_base}..{z_build_commit}",
    )
    candidate_commit_ids = {commit for commit, _ in candidate_commits}

    target_key = z_issue_key.upper()
    matches: list[str] = []
    for commit, message in candidate_commits:
        keys = resolves_keys(message)
        if target_key not in keys:
            continue
        if keys != {target_key}:
            raise InheritCandidateError(
                f"Commit {commit} resolves other Jira issues in addition to {target_key}"
            )
        matches.append(commit)

    if not matches:
        historic_matches = await _commit_messages(
            clone_path,
            z_build_commit,
            grep=target_key,
        )
        for commit, message in historic_matches:
            if commit in candidate_commit_ids:
                continue
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


async def inspect_commit_files(
    clone_path: Path,
    commit_sha: str,
    package: str,
) -> list[CommitFile]:
    """Validate the file inventory of a candidate packaging commit.

    The first version of the fast path supports regular additions and modifications
    of the package spec and locally declared patch files. Anything that could carry
    unrelated source or packaging state is deliberately left to the normal backport.
    """
    raw_inventory, _ = await check_subprocess(
        [
            "git",
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            f"{commit_sha}^",
            commit_sha,
        ],
        cwd=clone_path,
    )
    fields = raw_inventory.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise InheritCandidateError(f"Cannot parse changed files for {commit_sha}")

    inventory = [
        CommitFile(status=fields[index], path=fields[index + 1]) for index in range(0, len(fields), 2)
    ]
    if not inventory:
        raise InheritCandidateError(f"Commit {commit_sha} changes no files")
    if any(item.status not in {"A", "M"} for item in inventory):
        raise InheritCandidateError(f"Commit {commit_sha} contains a rename, deletion, or copy")

    spec_name = f"{package}.spec"
    z_spec, _ = await check_subprocess(
        ["git", "show", f"{commit_sha}:{spec_name}"],
        cwd=clone_path,
    )
    with Specfile(content=z_spec, sourcedir=clone_path) as spec:
        declared_patches = {
            patch.filename for patch in get_all_patches(spec) if patch.valid and patch.filename
        }

    for item in inventory:
        path = Path(item.path)
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
            raise InheritCandidateError(f"Commit {commit_sha} changes an unsafe path: {item.path}")
        if item.path != spec_name and item.path not in declared_patches:
            raise InheritCandidateError(f"Commit {commit_sha} changes unsupported packaging file {item.path}")
        numstat, _ = await check_subprocess(
            ["git", "diff", "--numstat", f"{commit_sha}^", commit_sha, "--", item.path],
            cwd=clone_path,
        )
        if numstat.startswith("-\t-\t"):
            raise InheritCandidateError(f"Commit {commit_sha} changes binary file {item.path}")

    return inventory


def _spec_safety_snapshot(content: str, sourcedir: Path) -> _SpecSafetySnapshot:
    with Specfile(content=content, sourcedir=sourcedir) as spec:
        sources = tuple(
            (source.number, source.location)
            for source in get_all_sources(spec)
            if source.valid and source.location
        )
        name = spec.expanded_name
        epoch = str(spec.expanded_epoch or 0)
        version = spec.expanded_version
        release = (spec.raw_release, spec.expanded_release)
    match = re.search(r"(?ms)^%changelog\b.*", content)
    return _SpecSafetySnapshot(
        name=name,
        epoch=epoch,
        version=version,
        release=release,
        sources=sources,
        changelog=match.group(0) if match else "",
    )


def _macro_int(value: object, default: int) -> int:
    if value is None:
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise InheritCandidateError(f"Unsupported macro option value: {value}") from exc


def _patch_applications(spec: Specfile, patch_number: int) -> list[_PatchApplication]:
    applications: list[_PatchApplication] = []
    with spec.prep() as prep:
        if prep is None:
            raise InheritCandidateError("Spec file has no %prep section")
        for macro in prep.macros:
            if isinstance(macro, AutosetupMacro):
                if not macro.options.N:
                    applications.append(_PatchApplication(strip=_macro_int(macro.options.p, 1)))
            elif isinstance(macro, AutopatchMacro):
                minimum = _macro_int(macro.options.m, 0)
                maximum = _macro_int(macro.options.M, 2**31 - 1)
                if minimum <= patch_number <= maximum:
                    applications.append(_PatchApplication(strip=_macro_int(macro.options.p, 1)))
            elif isinstance(macro, PatchMacro) and macro.number == patch_number:
                applications.append(_PatchApplication(strip=_macro_int(macro.options.p, 0)))
    return applications


def _validate_patch_usage(spec_path: Path, patch_files: list[str]) -> None:
    with Specfile(spec_path) as spec:
        valid_patches = [patch for patch in get_all_patches(spec) if patch.valid and patch.location]
        for patch_file in patch_files:
            declarations = [patch for patch in valid_patches if patch.location == patch_file]
            if len(declarations) != 1:
                raise InheritCandidateError(
                    f"Inherited patch {patch_file} must have exactly one active Patch declaration"
                )
            applications = _patch_applications(spec, declarations[0].number)
            if len(applications) != 1:
                raise InheritCandidateError(
                    f"Inherited patch {patch_file} must be applied exactly once in %prep"
                )


async def verify_inherited_patches(clone_path: Path, change: IntegratedChange) -> None:
    """Require every inherited patch to remain byte-for-byte equal to its source Git blob."""
    for patch_file, expected_blob in change.patch_blob_ids.items():
        path = clone_path / patch_file
        if not path.is_file():
            raise ImmutablePatchError(f"Inherited patch {patch_file} is missing")
        actual_blob, _ = await check_subprocess(
            ["git", "hash-object", "--", patch_file],
            cwd=clone_path,
        )
        if actual_blob.strip().lower() != expected_blob.lower():
            raise ImmutablePatchError(f"Inherited patch {patch_file} was modified")


async def validate_inherited_adaptation(
    clone_path: Path,
    package: str,
    saved_head: str,
    change: IntegratedChange,
) -> None:
    """Audit LLM changes before release/changelog metadata is added."""
    await verify_inherited_patches(clone_path, change)
    spec_name = f"{package}.spec"
    original_spec, _ = await check_subprocess(
        ["git", "show", f"{saved_head}:{spec_name}"],
        cwd=clone_path,
    )
    target_spec_path = clone_path / spec_name
    current_spec = target_spec_path.read_text()

    before = _spec_safety_snapshot(original_spec, clone_path)
    after = _spec_safety_snapshot(current_spec, clone_path)
    protected_fields = {
        "Name": (before.name, after.name),
        "Epoch": (before.epoch, after.epoch),
        "Version": (before.version, after.version),
        "Release": (before.release, after.release),
        "Source": (before.sources, after.sources),
        "%changelog": (before.changelog, after.changelog),
    }
    changed_protected = [name for name, values in protected_fields.items() if values[0] != values[1]]
    if changed_protected:
        raise InheritCandidateError(
            "Inheritance adaptation changed protected spec metadata: " + ", ".join(changed_protected)
        )

    changed_output, _ = await check_subprocess(
        ["git", "diff", "--name-only", saved_head, "--"],
        cwd=clone_path,
    )
    untracked_output, _ = await check_subprocess(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=clone_path,
    )
    ignored_output, _ = await check_subprocess(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard"],
        cwd=clone_path,
    )
    changed_files = {
        path
        for path in [
            *(changed_output or "").splitlines(),
            *(untracked_output or "").splitlines(),
            *(ignored_output or "").splitlines(),
        ]
        if path
    }
    allowed_files = {spec_name, *change.patch_files}
    unexpected = changed_files - allowed_files
    if unexpected:
        raise InheritCandidateError(f"Inheritance adaptation changed unsupported files: {sorted(unexpected)}")
    _validate_patch_usage(target_spec_path, change.patch_files)
    if change.source_spec_changed and not change.patch_files and current_spec == original_spec:
        raise InheritCandidateError("Inheritance adaptation did not apply the source spec change")
    if not changed_files:
        raise InheritCandidateError("Inheritance adaptation produced no changes")


async def apply_zstream_change(
    clone_path: Path,
    package: str,
    commit_sha: str,
) -> IntegratedChange:
    """Materialize immutable patches and source context without changing the target spec."""
    inventory = await inspect_commit_files(clone_path, commit_sha, package)
    spec_name = f"{package}.spec"
    commit_message, _ = await check_subprocess(
        ["git", "log", "-1", "--format=%B", commit_sha],
        cwd=clone_path,
    )
    source_spec_diff, _ = await check_subprocess(
        ["git", "diff", f"{commit_sha}^", commit_sha, "--", spec_name],
        cwd=clone_path,
    )
    patch_files = [item.path for item in inventory if item.path != spec_name]
    patch_blob_ids: dict[str, str] = {}
    if patch_files:
        await check_subprocess(
            ["git", "restore", "--source", commit_sha, "--worktree", "--", *patch_files],
            cwd=clone_path,
        )
        for patch_file in patch_files:
            blob_id, _ = await check_subprocess(
                ["git", "rev-parse", f"{commit_sha}:{patch_file}"],
                cwd=clone_path,
            )
            patch_blob_ids[patch_file] = blob_id.strip().lower()

    change = IntegratedChange(
        commit_sha=commit_sha,
        commit_message=commit_message.rstrip(),
        changed_files=[spec_name, *patch_files],
        patch_files=patch_files,
        patch_blob_ids=patch_blob_ids,
        source_spec_diff=source_spec_diff,
        source_spec_changed=any(item.path == spec_name for item in inventory),
    )
    await verify_inherited_patches(clone_path, change)
    return change


async def reset_inherit_attempt(clone_path: Path, saved_head: str, introduced_files: list[str]) -> None:
    """Restore the exact clean target checkout after a failed pre-push attempt."""
    await run_subprocess(["git", "cherry-pick", "--abort"], cwd=clone_path)
    await run_subprocess(["git", "cherry-pick", "--quit"], cwd=clone_path)
    await check_subprocess(["git", "reset", "--hard", saved_head], cwd=clone_path)
    parent_directories: set[Path] = set()
    for relative_path in introduced_files:
        path = clone_path / relative_path
        if (path.is_file() or path.is_symlink()) and not await _is_tracked(clone_path, relative_path):
            path.unlink()
            parent_directories.update(path.parents)
    for directory in sorted(
        (path for path in parent_directories if path != clone_path and clone_path in path.parents),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        with suppress(OSError):
            directory.rmdir()
    status, _ = await check_subprocess(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=clone_path,
    )
    if (status or "").strip():
        raise InheritCandidateError(f"Inheritance cleanup left a dirty checkout: {status.strip()}")


async def _is_tracked(clone_path: Path, relative_path: str) -> bool:
    exit_code, _, _ = await run_subprocess(
        ["git", "ls-files", "--error-unmatch", "--", relative_path],
        cwd=clone_path,
    )
    return exit_code == 0


def rewrite_commit_message(commit_message: str, z_issue_key: str, y_issue_key: str) -> str:
    """Retarget exact Jira footer references while preserving the original message."""
    key_pattern = re.compile(rf"(?<![A-Z0-9-]){re.escape(z_issue_key)}(?![A-Z0-9-])", re.IGNORECASE)
    lines: list[str] = []
    saw_y_resolves = False
    for line in commit_message.rstrip().splitlines():
        footer = re.match(r"^(Resolves|Related):(.*)$", line, re.IGNORECASE)
        if footer:
            line = f"{footer.group(1)}:{key_pattern.sub(y_issue_key, footer.group(2))}"
            if footer.group(1).lower() == "resolves" and y_issue_key.upper() in {
                key.upper() for key in _JIRA_KEY_RE.findall(line)
            }:
                saw_y_resolves = True
        lines.append(line)
    if not saw_y_resolves:
        lines.extend(["", f"Resolves: {y_issue_key}"])
    return "\n".join(lines).rstrip()


def ensure_single_ymir_attribution(commit_message: str) -> str:
    """Add Ymir attribution when absent and collapse duplicate Ymir trailers."""
    lines: list[str] = []
    saw_attribution = False
    for line in commit_message.rstrip().splitlines():
        if _YMIR_ATTRIBUTION_RE.fullmatch(line):
            if saw_attribution:
                continue
            saw_attribution = True
        lines.append(line)

    message = "\n".join(lines).rstrip()
    if not saw_attribution:
        message = f"{message}\n\nAssisted-by: Ymir" if message else "Assisted-by: Ymir"
    return f"{message}\n"
