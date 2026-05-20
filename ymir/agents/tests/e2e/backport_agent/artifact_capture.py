"""Capture backport workflow artifacts to disk for inspection and evaluation.

After the backport workflow completes, these functions extract git diffs,
spec files, patch files, and structured results from the finished state
and persist them in a directory tree that can be inspected offline or fed
to an LLM judge for evaluation.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import git

logger = logging.getLogger(__name__)


@dataclass
class CapturedArtifacts:
    """Container for artifacts extracted from a finished workflow state."""

    output_dir: Path
    commit_diff: str | None = None
    spec_content: str | None = None
    patch_files: dict[str, str] = field(default_factory=dict)
    result_json: dict | None = None
    log_json: dict | None = None


def _new_patch_files(repo_path: Path) -> list[Path]:
    """Return patch files added or modified by the last commit."""
    try:
        repo = git.Repo(repo_path)
        changed = repo.git.diff("HEAD~1", "HEAD", "--name-only").splitlines()
        return [
            Path(repo_path) / f for f in changed if f.endswith(".patch") and (Path(repo_path) / f).is_file()
        ]
    except Exception as exc:
        logger.warning("Could not list new patches from %s: %s", repo_path, exc)
    return []


def _git_diff_last_commit(repo_path: Path) -> str | None:
    """Return the diff of the most recent commit, or None on failure."""
    try:
        repo = git.Repo(repo_path)
        diff = repo.git.diff("HEAD~1", "HEAD")
        return diff if diff.strip() else None
    except Exception as exc:
        logger.warning("Could not extract git diff from %s: %s", repo_path, exc)
    return None


def capture_backport_artifacts(
    state,
    output_dir: Path,
) -> CapturedArtifacts:
    """Extract and save artifacts from a finished backport workflow state.

    Args:
        state: The finished ``BackportState`` instance (must have
            ``local_clone``, ``backport_result``, ``log_result``, and
            ``package`` attributes).
        output_dir: Root directory where artifacts will be written.
            A subdirectory named after the Jira issue is created
            automatically.

    Returns:
        A ``CapturedArtifacts`` instance with all extracted data.
    """
    issue_dir = output_dir / state.jira_issue
    issue_dir.mkdir(parents=True, exist_ok=True)

    artifacts = CapturedArtifacts(output_dir=issue_dir)

    if state.local_clone and Path(state.local_clone).is_dir():
        diff = _git_diff_last_commit(state.local_clone)
        if diff:
            artifacts.commit_diff = diff
            (issue_dir / "commit.diff").write_text(diff)

        spec_path = Path(state.local_clone) / f"{state.package}.spec"
        if spec_path.is_file():
            content = spec_path.read_text()
            artifacts.spec_content = content
            (issue_dir / "spec_file.spec").write_text(content)

        patches_dir = issue_dir / "patches"
        patches_dir.mkdir(exist_ok=True)
        for p in _new_patch_files(state.local_clone):
            content = p.read_text()
            artifacts.patch_files[p.name] = content
            (patches_dir / p.name).write_text(content)

    if state.backport_result:
        result_data = json.loads(state.backport_result.model_dump_json())
        artifacts.result_json = result_data
        (issue_dir / "backport_result.json").write_text(json.dumps(result_data, indent=2))

    if state.log_result:
        log_data = json.loads(state.log_result.model_dump_json())
        artifacts.log_json = log_data
        (issue_dir / "log_result.json").write_text(json.dumps(log_data, indent=2))

    logger.info("Captured backport artifacts for %s in %s", state.jira_issue, issue_dir)
    return artifacts
