#!/usr/bin/env python3
"""Pre-push hook to auto-bump patch version in subpackages with code changes.

Compares the commits being pushed against the remote base. For each configured
package, if there are code changes but the version hasn't been bumped yet,
bumps the patch version, commits, and aborts the push so the user can push again
with the bump included.
"""

import sys
from pathlib import Path

import git
import tomlkit

PACKAGES = [
    Path("ymir/tools"),
    Path("ymir/common"),
]


def get_base(repo: git.Repo, local_sha: str, remote_sha: str) -> git.Commit | None:
    if remote_sha != "0" * 40:
        return repo.commit(remote_sha)
    merge_base = repo.merge_base(local_sha, "main")
    return merge_base[0] if merge_base else None


def get_version_at_ref(commit: git.Commit, pyproject: Path) -> str | None:
    try:
        blob = commit.tree / str(pyproject)
    except KeyError:
        return None
    data = tomlkit.loads(blob.data_stream.read().decode())
    return data.get("project", {}).get("version")


def has_code_changes(repo: git.Repo, directory: Path, local: git.Commit, base: git.Commit) -> bool:
    diff = base.diff(local, paths=[str(directory)])
    return any(not (d.a_path or d.b_path).endswith("pyproject.toml") for d in diff)


def main() -> int:
    push_refs = []
    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) >= 4:
            push_refs.append((parts[1], parts[3]))

    if not push_refs:
        return 0

    repo = git.Repo(search_parent_directories=True)
    bumped = []

    for local_sha, remote_sha in push_refs:
        if local_sha == "0" * 40:
            continue

        local = repo.commit(local_sha)
        base = get_base(repo, local_sha, remote_sha)
        if not base:
            continue

        for package in PACKAGES:
            pyproject = package / "pyproject.toml"

            if not has_code_changes(repo, package, local, base):
                continue

            base_version = get_version_at_ref(base, pyproject)

            data = tomlkit.loads(pyproject.read_text())
            local_version = data["project"]["version"]

            if local_version != base_version:
                continue

            major, minor, patch = local_version.split(".")
            new_version = f"{major}.{minor}.{int(patch) + 1}"
            data["project"]["version"] = new_version
            pyproject.write_text(tomlkit.dumps(data))

            bumped.append((package, f"{local_version} -> {new_version}"))

    if not bumped:
        return 0

    files = [str(pkg / "pyproject.toml") for pkg, _ in bumped]
    repo.index.add(files)

    details = ", ".join(f"{pkg.name} ({ver})" for pkg, ver in bumped)
    repo.index.commit(f"Bump package version: {details}")

    print(
        f"Auto-bumped version: {details}. Please push again.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
