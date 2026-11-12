"""Run the Amalgamator arm of the backport A/B for one issue.

This is the e2e glue, not a production workflow step. It takes the inputs Ymir
already has for a backport — package, version, the upstream fix URL, and an
optional description — prepares a clean upstream clone, and hands everything to
Amalgamator through :mod:`ymir.agents.amalgamator.adapter`.

Two things this module deliberately does not do:

* it does not touch the agent's own clone. ``{local_clone}-upstream`` is left
  alone; the arm clones afresh under its own work directory, so a run can be
  compared with the agent's without either influencing the other;
* it does not represent dist-git's existing patches. Amalgamator ports onto a
  pristine upstream release commit, so its patch is the pure upstream backport.
  That is the part it is meant to be good at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ymir.agents.amalgamator import adapter
from ymir.agents.amalgamator import inputs as arm_inputs

logger = logging.getLogger(__name__)


class ArmUnavailableError(RuntimeError):
    """Raised when the arm cannot be set up from the available inputs."""


@dataclass
class ArmInputs:
    """Everything Amalgamator needs for one issue."""

    issue_key: str
    package: str
    version: str
    upstream_patch_url: str
    upstream_clone: Path
    workdir: Path
    base_commit: str
    description: str
    source_ref: str
    fix_commit: str | None = None
    fix_patch: Path | None = None


def default_description(package: str, issue_key: str) -> str:
    """Fallback description when the ticket summary is not available.

    In mock mode the summary lives in ``$JIRA_MOCK_FILES/<issue>``. In the
    public repository that directory is empty, so the e2e arm falls back to a
    sentence naming the package and the ticket.
    """
    return f"Backport the upstream fix for {package} tracked as {issue_key}"


def collect_inputs(
    *,
    issue_key: str,
    package: str,
    version: str,
    upstream_patch_url: str,
    workdir: Path | str,
    description: str | None = None,
    dist_git_patch: Path | None = None,
    fix_commit: str | None = None,
) -> ArmInputs:
    """Prepare a clean upstream clone and resolve the base commit.

    ``fix_commit`` is resolved from the patch URL when not given: directly for
    commit URLs, through the forge API for pull/merge-request URLs (whose
    ``.patch`` is a multi-message mbox Amalgamator rejects). ``dist_git_patch``
    is the upstream fix patch already downloaded next to the dist-git spec
    (``<jira>-0.patch``) and is used only when no commit can be resolved.

    Raises:
        ArmUnavailableError: when the URL does not identify an upstream
            repository, the clone fails, or no release tag matches *version*.
    """
    workdir = Path(workdir).resolve()
    repo_url = arm_inputs.upstream_repo_url(upstream_patch_url)
    if repo_url is None:
        raise ArmUnavailableError(
            f"Cannot derive an upstream repository from patch URL: {upstream_patch_url}"
        )

    # Amalgamator treats ``source_path`` as the pristine repository and does
    # its work in ``<workdir>/<repo-name>``. Keep the clone out of that path so
    # a run can never reset the clone it was given.
    clone_path = workdir / "source" / package
    try:
        arm_inputs.clone_upstream(repo_url, clone_path)
    except RuntimeError as exc:
        raise ArmUnavailableError(str(exc)) from exc

    base_commit = arm_inputs.resolve_base_commit(clone_path, version, package)
    if base_commit is None:
        raise ArmUnavailableError(
            f"No upstream tag matches version {version} in {repo_url}; cannot pick a base commit"
        )

    if fix_commit is None:
        fix_commit = arm_inputs.resolve_fix_commit(upstream_patch_url)
    if (
        fix_commit is not None
        and not arm_inputs.commit_exists(clone_path, fix_commit)
        and not arm_inputs.fetch_commit(clone_path, fix_commit)
    ):
        raise ArmUnavailableError(f"Fix commit {fix_commit} is not reachable from {repo_url}")
    fix_patch = dist_git_patch
    if fix_commit is None:
        if fix_patch is None or not Path(fix_patch).is_file():
            raise ArmUnavailableError("Patch URL names no commit and no downloaded patch was supplied")
        fix_patch = Path(fix_patch).resolve()

    return ArmInputs(
        issue_key=issue_key,
        package=package,
        version=version,
        upstream_patch_url=upstream_patch_url,
        upstream_clone=clone_path,
        workdir=workdir,
        base_commit=base_commit,
        description=description or default_description(package, issue_key),
        source_ref=arm_inputs.default_branch_ref(clone_path),
        fix_commit=fix_commit,
        fix_patch=fix_patch if fix_commit is None else None,
    )


def run_arm(
    arm: ArmInputs,
    *,
    amalgamator_root: Path | str,
    model: str | None = None,
    models_path: str | None = None,
    build_command: str = "",
    test_command: str = "",
    timeout: int = adapter.DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
) -> adapter.AmalgamatorResult:
    """Run Amalgamator for one prepared issue and return its result."""
    logger.info(
        "Amalgamator arm: %s %s base=%s fix=%s",
        arm.issue_key,
        arm.package,
        arm.base_commit[:12],
        (arm.fix_commit or str(arm.fix_patch))[:24],
    )
    return adapter.run_case(
        amalgamator_root=amalgamator_root,
        issue_id=arm.issue_key,
        description=arm.description,
        upstream_clone=arm.upstream_clone,
        workdir=arm.workdir,
        base_commit=arm.base_commit,
        fix_patch=None if arm.fix_patch is None else str(arm.fix_patch),
        fix_commit=arm.fix_commit,
        source_branch=arm.source_ref,
        build_command=build_command,
        test_command=test_command,
        model=model,
        models_path=models_path,
        timeout=timeout,
        python=python,
    )
