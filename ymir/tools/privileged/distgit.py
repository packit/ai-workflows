import asyncio
import logging
import os
import random
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import git
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field
from specfile import Specfile

from ymir.common.base_utils import init_kerberos_ticket
from ymir.common.utils import NoBuildFoundError, get_latest_buildroot_build, get_latest_z_pending_build
from ymir.common.version_utils import parse_zstream_branch_name
from ymir.tools.base import CloneableTool as Tool
from ymir.tools.base import make_additional_context, tool_error_context
from ymir.tools.constants import ZSTREAM_BRANCH_POLICY_REJECTED
from ymir.tools.errors import ToolErrorWithContext
from ymir.tools.privileged.utils import sanitize_url

logger = logging.getLogger(__name__)

SYNC_TIMEOUT = 1 * 60 * 60  # seconds
_TRANSIENT_MAX_RETRIES = 3
_TRANSIENT_BASE_DELAY = 5  # seconds

_TRANSIENT_STDERR_PATTERNS = (
    "connection closed",
    "connection reset",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "broken pipe",
    "ssh_exchange_identification",
)

_T = TypeVar("_T")

# A commit is checked against the target branch's dist-git check-in policy by
# the gitbzverify update hook. When the commit references issues whose
# FixVersion does not match the target branch, the hook declines the push.
_UNAPPROVED_ISSUE_RE = re.compile(
    r"(?P<issue>[A-Z][A-Z0-9]+-\d+)\s+\(FixVersion is set to:\s*(?P<fix_version>[\w.+-]+)\s*\)"
)
# Fix versions are restricted to plain version characters so hook output cannot
# inject Jira wiki markup into the comment.
_POLICY_TARGET_RE = re.compile(r"jiraField\(fixVersions\)\s*==\s*(?P<fix_version>[\w.+-]+)")
_MAX_LISTED_POLICY_ISSUES = 5


@dataclass(frozen=True)
class CheckinPolicyRejection:
    """Issues and policy targets parsed from a dist-git check-in rejection."""

    unapproved: tuple[tuple[str, str], ...]
    target_fix_versions: tuple[str, ...]


def _parse_checkin_policy_rejection(stderr: str) -> CheckinPolicyRejection | None:
    """Return parsed details when *stderr* is a dist-git check-in policy rejection.

    Returns ``None`` for unrelated push failures so callers can keep surfacing
    the original error.
    """
    is_policy_rejection = "hook declined" in stderr and (
        "Current checkin policy requires" in stderr or _UNAPPROVED_ISSUE_RE.search(stderr) is not None
    )
    if not is_policy_rejection:
        return None

    unapproved: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _UNAPPROVED_ISSUE_RE.finditer(stderr):
        issue = match.group("issue")
        fix_version = match.group("fix_version")
        if (issue, fix_version) not in seen:
            seen.add((issue, fix_version))
            unapproved.append((issue, fix_version))

    targets = tuple(dict.fromkeys(_POLICY_TARGET_RE.findall(stderr)))
    return CheckinPolicyRejection(tuple(unapproved), targets)


def _jira_code(value: str) -> str:
    """Return Jira wiki-markup monospace text."""
    return "{{" + value + "}}"


def _format_checkin_policy_rejection(
    *,
    package: str,
    branch: str,
    ref: str,
    build_ref: str,
    lookup_tag: str,
    source_branch: str | None,
    ref_in_source_branch: bool | None,
    rejection: CheckinPolicyRejection,
) -> str:
    """Build the maintainer-facing explanation for a rejected branch push."""
    fix_versions = sorted({fix_version for _issue, fix_version in rejection.unapproved})
    if ref == build_ref:
        base_description = (
            f"The branch would be based on commit {_jira_code(ref[:12])}, taken from the "
            f"{_jira_code(lookup_tag)} tag."
        )
    else:
        base_description = (
            f"The branch would be based on commit {_jira_code(ref[:12])}, the latest commit with "
            f"the same NVR as build commit {_jira_code(build_ref[:12])} from the "
            f"{_jira_code(lookup_tag)} tag."
        )
    lines = [
        f"Ymir could not create the missing Z-stream branch {_jira_code(branch)} for {_jira_code(package)}.",
        "",
        base_description,
    ]

    if source_branch:
        if ref_in_source_branch is False:
            lines.append(f"That commit is not part of {_jira_code(source_branch)}.")
        elif ref_in_source_branch is True:
            lines.append(
                f"That commit is part of {_jira_code(source_branch)}, but it references issues "
                f"that are not accepted for {_jira_code(branch)}."
            )

    if rejection.unapproved:
        if len(fix_versions) == 1:
            lines.append(f"It references issues targeted at {_jira_code(fix_versions[0])}:")
        else:
            lines.append(
                "It references issues targeted at other streams ("
                + ", ".join(_jira_code(v) for v in fix_versions)
                + "):"
            )
        listed = rejection.unapproved[:_MAX_LISTED_POLICY_ISSUES]
        if len(fix_versions) == 1:
            issue_text = ", ".join(issue for issue, _fix_version in listed)
        else:
            issue_text = ", ".join(f"{issue} ({fix_version})" for issue, fix_version in listed)
        remaining = len(rejection.unapproved) - len(listed)
        if remaining > 0:
            issue_text += f", ... and {remaining} more"
        lines.append(issue_text)
    else:
        lines.append("The dist-git hook did not list the individual issues in a format Ymir could parse.")

    if rejection.target_fix_versions:
        targets = ", ".join(_jira_code(v) for v in rejection.target_fix_versions)
        lines.extend(
            [
                "",
                (
                    f"The {_jira_code(branch)} check-in policy only accepts issues with "
                    f"fixVersion {targets}, so dist-git's {_jira_code('gitbzverify')} hook "
                    "declined the push."
                ),
            ]
        )
    else:
        lines.extend(["", f"Dist-git's {_jira_code('gitbzverify')} hook declined the push."])

    if source_branch is None:
        lines.extend(
            [
                "",
                (
                    "Ymir could not find a newer Z-stream or a CentOS Stream branch to base "
                    "the new branch on, so it used the build commit directly."
                ),
            ]
        )
    elif ref_in_source_branch is False and source_branch.endswith("-main"):
        lines.extend(
            [
                "",
                (
                    "This usually means the branches have not been evened out: the branch "
                    "containing the build commit has changes that are not in "
                    f"{_jira_code(source_branch)} and are not accepted for {_jira_code(branch)}."
                ),
            ]
        )
    elif ref_in_source_branch is False:
        lines.extend(
            [
                "",
                (
                    f"The build commit is not part of the newer Z-stream "
                    f"{_jira_code(source_branch)} that Ymir considered. Do not create "
                    f"{_jira_code(branch)} from {_jira_code(source_branch)}: it contains "
                    f"fixes that are not accepted for {_jira_code(branch)}."
                ),
            ]
        )
    elif ref_in_source_branch is None:
        lines.extend(
            [
                "",
                (
                    f"Ymir could not verify whether the commit belongs to "
                    f"{_jira_code(source_branch)}. Review the branch contents before retrying."
                ),
            ]
        )

    if source_branch is None:
        action = (
            f"# Create {_jira_code(branch)} manually once an appropriate source branch "
            "exists in the repository."
        )
    elif ref_in_source_branch is True:
        action = (
            f"# Review whether the referenced fixes should target "
            f"{_jira_code(branch)}, or correct the branch contents so the new branch can "
            "be based on an accepted commit."
        )
    elif ref_in_source_branch is None and source_branch.endswith("-main"):
        action = (
            f"# Check whether commit {_jira_code(ref[:12])} is part of {_jira_code(source_branch)}. "
            "If it is not, even out the RHEL Z-stream and CentOS Stream branches using your usual "
            f"sync process; otherwise create {_jira_code(branch)} manually from an accepted base."
        )
    elif source_branch.endswith("-main"):
        action = (
            "# Even out the RHEL Z-stream and CentOS Stream branches using your usual "
            f"sync process, or create {_jira_code(branch)} manually from an accepted base."
        )
    else:
        action = (
            f"# Create {_jira_code(branch)} manually from an accepted base, not from "
            f"{_jira_code(source_branch)}."
        )
    lines.extend(
        [
            "",
            "To unblock Ymir:",
            action,
            f"# Add the {_jira_code('ymir_todo')} label to this issue to retry.",
        ]
    )
    return "\n".join(lines)


def _is_transient_git_error(exc: Exception) -> bool:
    if not isinstance(exc, git.exc.GitCommandError):
        return False
    stderr = str(exc.stderr or "").lower()
    return any(p in stderr for p in _TRANSIENT_STDERR_PATTERNS)


async def _retry_transient(
    fn: Callable[[], Awaitable[_T]],
    label: str,
    max_retries: int = _TRANSIENT_MAX_RETRIES,
    base_delay: int = _TRANSIENT_BASE_DELAY,
    retry_on_empty: bool = False,
) -> _T:
    for attempt in range(max_retries):
        try:
            result = await fn()
        except Exception as e:
            if attempt < max_retries - 1 and _is_transient_git_error(e):
                backoff = random.uniform(0, base_delay * 2**attempt)  # noqa: S311
                logger.warning(
                    f"{label} failed (attempt {attempt + 1}/{max_retries}): "
                    f"{sanitize_url(str(e))}; retrying in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)
            else:
                raise
        else:
            if retry_on_empty and not result and attempt < max_retries - 1:
                backoff = random.uniform(0, base_delay * 2**attempt)  # noqa: S311
                logger.warning(
                    f"{label} returned empty (attempt {attempt + 1}/{max_retries}); "
                    f"retrying in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)
            else:
                return result
    raise AssertionError("unreachable")


class CreateZstreamBranchToolInput(BaseModel):
    package: str = Field(description="Package name")
    branch: str = Field(description="Name of the branch to create")


class CreateZstreamBranchTool(Tool[CreateZstreamBranchToolInput, ToolRunOptions, StringToolOutput]):
    name = "create_zstream_branch"
    timeout = 10800
    description = """
    Creates a new Z-Stream branch for the specified package in internal dist-git.
    """
    input_schema = CreateZstreamBranchToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "distgit", self.name],
            creator=self,
        )

    @staticmethod
    def _find_source_branch(repo: git.Repo, branch: str) -> str | None:
        if not (parsed := parse_zstream_branch_name(branch)):
            return None
        major, minor_str = parsed
        minor = int(minor_str)
        remote_branches = {ref.name.split("/")[-1] for ref in repo.remotes.origin.refs}
        higher_branch = sorted(
            (m, ref_name)
            for ref_name in remote_branches
            if (p := parse_zstream_branch_name(ref_name)) and p[0] == major and (m := int(p[1])) > minor
        )
        if higher_branch:
            return higher_branch[0][1]
        if (main_branch := f"rhel-{major}-main") in remote_branches:
            return main_branch
        return None

    @staticmethod
    async def _find_latest_same_nvr_ref(
        repo: git.Repo,
        package: str,
        build_ref: str,
        source_branch: str,
    ) -> str:
        """
        Iterates through commits of source_branch, starting from build_ref, and returns
        the latest commit that shares the same NVR, to include various fixups etc.
        In most cases head of source_branch will be equal to build_ref and this method
        does nothing.

        Args:
            repo: Repo object representing dist-git
            package: Package name
            build_ref: Git ref that the latest candidate build corresponding to the target branch
              originated from
            source_branch: Branch that should be used as a source - higher Z-Stream or rhel-X-main

        Returns:
            Ref to base the new Z-Stream branch on.
        """

        main_ref = f"origin/{source_branch}"

        try:
            is_ancestor = await asyncio.to_thread(repo.is_ancestor, build_ref, main_ref)
        except git.exc.GitCommandError as e:
            logger.debug(f"Failed to check ancestry between {build_ref} and {main_ref}: {e}")
            return build_ref

        if not is_ancestor:
            logger.debug(f"{build_ref} is not an ancestor of {main_ref}, skipping NVR walk")
            return build_ref

        def walk_nvr():
            def evr_at(rev):
                try:
                    commit = repo.commit(rev) if isinstance(rev, str) else rev
                    # Discover the spec in this revision, independent of the checkout
                    # and any spec renames between the build and subsequent fixups.
                    specs = {
                        blob.name: blob
                        for blob in commit.tree.blobs
                        if blob.name.endswith(".spec") and not blob.name.startswith(".")
                    }
                    spec_blob = specs.get(f"{package}.spec")
                    if spec_blob is None:
                        if len(specs) != 1:
                            return None
                        spec_blob = next(iter(specs.values()))
                    content = spec_blob.data_stream.read().decode("utf-8", errors="replace")
                    # sourcedir is required when passing content, but its value doesn't matter in this case
                    with Specfile(content=content, sourcedir="/") as spec:
                        return spec.expanded_epoch, spec.expanded_version, spec.expanded_release
                except Exception:
                    return None

            if (build_evr := evr_at(build_ref)) is None:
                return build_ref

            latest_same_nvr = build_ref
            for commit in repo.iter_commits(f"{build_ref}..{main_ref}", ancestry_path=True, reverse=True):
                if evr_at(commit) == build_evr:
                    latest_same_nvr = commit.hexsha
                else:
                    break
            return latest_same_nvr

        latest_same_nvr = await asyncio.to_thread(walk_nvr)
        if latest_same_nvr != build_ref:
            logger.info(f"Advanced ref from {build_ref[:12]} to {latest_same_nvr[:12]} (same NVR)")
        return latest_same_nvr

    async def _run(
        self,
        tool_input: CreateZstreamBranchToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        package = tool_input.package
        branch = tool_input.branch
        with tool_error_context("Failed to initialize Kerberos ticket"):
            principal = await init_kerberos_ticket()
        username = principal.split("@", maxsplit=1)[0]
        token = os.environ["GITLAB_TOKEN"]
        gitlab_repo_url = f"https://oauth2:{token}@gitlab.com/redhat/rhel/rpms/{package}"
        with tool_error_context("Failed to check GitLab remote", package=package, branch=branch):
            if await _retry_transient(
                lambda: asyncio.to_thread(git.cmd.Git().ls_remote, gitlab_repo_url, branch, branches=True),
                f"ls_remote GitLab {package}/{branch}",
            ):
                return StringToolOutput(
                    result=f"Z-Stream branch {branch} already exists, no need to create it"
                )
        with (
            tool_error_context(
                "Failed to create Z-Stream branch",
                package=package,
                branch=branch,
            ),
            tempfile.TemporaryDirectory() as path,
        ):
            # Username is taken from the Kerberos principal and embedded in
            # the URL explicitly — do not rely on the SSH config User setting.
            clone_url = f"ssh://{username}@pkgs.devel.redhat.com/rpms/{package}"
            clone_dest = os.path.join(path, package)

            async def _clone():
                if os.path.exists(clone_dest):
                    shutil.rmtree(clone_dest)
                return await asyncio.to_thread(git.Repo.clone_from, clone_url, clone_dest)

            with tool_error_context(
                "Failed to clone dist-git repo", package=package, branch=branch, clone_url=clone_url
            ):
                repo = await _retry_transient(_clone, f"clone {package} from dist-git")

            branch_creation_details = None
            if branch in [ref.name.split("/")[-1] for ref in repo.remotes.origin.refs]:
                # Branch already exists in dist-git but not yet mirrored to GitLab.
                # This happens when a previous push succeeded server-side but the SSH
                # connection dropped before the client received the ACK. Skip the push
                # and fall through to poll GitLab for the sync.
                logger.warning(
                    f"Branch {branch} already exists in dist-git but not yet on GitLab; "
                    "skipping push and waiting for mirror sync"
                )
            else:
                lookup_tag = f"{branch}-z-pending"
                with tool_error_context(
                    "Failed to find suitable source build", package=package, branch=branch
                ):
                    try:
                        _, ref = await get_latest_z_pending_build(package, branch)
                    except NoBuildFoundError:
                        # this could be a buildroot-only package
                        lookup_tag = f"{branch}-buildrequires"
                        _, ref = await get_latest_buildroot_build(package, branch)
                build_ref = ref
                source_branch = self._find_source_branch(repo, branch)
                if source_branch and source_branch.endswith("-main"):
                    ref = await self._find_latest_same_nvr_ref(
                        repo,
                        package,
                        ref,
                        source_branch,
                    )
                    branch_creation_details = f"from {source_branch} at {ref[:12]}"
                else:
                    branch_creation_details = f"at {ref[:12]}"
                ref_in_source_branch = None
                if source_branch:
                    try:
                        ref_in_source_branch = await asyncio.to_thread(
                            repo.is_ancestor, ref, f"origin/{source_branch}"
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Could not check ancestry of {ref[:12]} against origin/{source_branch}: {exc}"
                        )
                with tool_error_context(
                    "Failed to push branch to dist-git", package=package, branch=branch, ref=ref
                ):
                    try:
                        await asyncio.to_thread(repo.commit, ref)
                    except Exception:
                        raise ToolError(
                            f"Commit {ref} (from latest Brew build) not found in dist-git clone of {package}"
                        ) from None
                    try:
                        await _retry_transient(
                            lambda: asyncio.to_thread(repo.git.push, "origin", f"{ref}:refs/heads/{branch}"),
                            f"push {branch} to dist-git",
                        )
                    except git.exc.GitCommandError as e:
                        stderr = e.stderr or ""
                        if rejection := _parse_checkin_policy_rejection(stderr):
                            logger.warning(
                                f"Dist-git check-in policy rejected {ref[:12]} branch "
                                f"{branch} for {package}: {sanitize_url(stderr)}"
                            )
                            message = _format_checkin_policy_rejection(
                                package=package,
                                branch=branch,
                                ref=ref,
                                build_ref=build_ref,
                                lookup_tag=lookup_tag,
                                source_branch=source_branch,
                                ref_in_source_branch=ref_in_source_branch,
                                rejection=rejection,
                            )
                            raise ToolErrorWithContext(
                                message,
                                cause=e,
                                additional_context=make_additional_context(
                                    error_code=ZSTREAM_BRANCH_POLICY_REJECTED,
                                    user_message=message,
                                    package=package,
                                    branch=branch,
                                    ref=ref,
                                    build_ref=build_ref,
                                    source_branch=source_branch or "",
                                    lookup_tag=lookup_tag,
                                    ref_in_source_branch=(
                                        "unknown"
                                        if ref_in_source_branch is None
                                        else "yes"
                                        if ref_in_source_branch
                                        else "no"
                                    ),
                                    unapproved_issue_count=len(rejection.unapproved),
                                    unapproved_fix_versions=", ".join(
                                        sorted({fix_version for _issue, fix_version in rejection.unapproved})
                                    ),
                                    target_fix_versions=", ".join(rejection.target_fix_versions),
                                    raw_stderr=sanitize_url(stderr)[:4000],
                                ),
                            ) from e
                        raise
                    if not await _retry_transient(
                        lambda: asyncio.to_thread(repo.git.ls_remote, "--heads", "origin", branch),
                        f"verify {branch} on dist-git",
                        retry_on_empty=True,
                    ):
                        raise ToolError(
                            f"Push appeared to succeed but branch {branch} not found "
                            f"on dist-git — possible silent rejection by server ACL"
                        )
            start_time = time.monotonic()
            while time.monotonic() - start_time < SYNC_TIMEOUT:
                try:
                    if await asyncio.to_thread(repo.git.ls_remote, gitlab_repo_url, branch, branches=True):
                        msg = f"Successfully created Z-Stream branch {branch}"
                        if branch_creation_details:
                            msg += f" ({branch_creation_details})"
                        return StringToolOutput(result=msg)
                except git.exc.GitCommandError as e:
                    if not _is_transient_git_error(e):
                        raise ToolError("Failed to poll GitLab mirror") from e
                    logger.warning(f"Transient error polling GitLab mirror sync: {sanitize_url(str(e))}")
                elapsed = int(time.monotonic() - start_time)
                logger.info(
                    f"Waiting for GitLab mirror sync of {package} branch {branch} ({elapsed}s elapsed)"
                )
                await asyncio.sleep(30)
            raise ToolErrorWithContext(
                "GitLab mirror sync timed out",
                additional_context=make_additional_context(
                    package=package,
                    branch=branch,
                    timeout=SYNC_TIMEOUT,
                ),
            )
