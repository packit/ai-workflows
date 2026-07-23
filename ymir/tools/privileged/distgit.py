import asyncio
import logging
import os
import random
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import git
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field
from specfile import Specfile

from ymir.common.base_utils import KerberosError, init_kerberos_ticket
from ymir.common.utils import get_latest_candidate_build, get_latest_z_pending_build
from ymir.common.version_utils import is_older_zstream, parse_zstream_branch_name
from ymir.tools.base import CloneableTool as Tool
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
    "failed to push some refs",
)

_T = TypeVar("_T")


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
) -> _T:
    for attempt in range(max_retries):
        try:
            return await fn()
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
    raise AssertionError("unreachable")


class CreateZstreamBranchToolInput(BaseModel):
    package: str = Field(description="Package name")
    branch: str = Field(description="Name of the branch to create")


class CreateZstreamBranchTool(Tool[CreateZstreamBranchToolInput, ToolRunOptions, StringToolOutput]):
    name = "create_zstream_branch"
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

        spec_filename = f"{package}.spec"
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
                    content = (
                        (commit.tree / spec_filename).data_stream.read().decode("utf-8", errors="replace")
                    )
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
        try:
            principal = await init_kerberos_ticket()
        except KerberosError as e:
            raise ToolError(f"Failed to initialize Kerberos ticket: {e}") from e
        username = principal.split("@", maxsplit=1)[0]
        token = os.environ["GITLAB_TOKEN"]
        gitlab_repo_url = f"https://oauth2:{token}@gitlab.com/redhat/rhel/rpms/{package}"
        try:
            if await _retry_transient(
                lambda: asyncio.to_thread(git.cmd.Git().ls_remote, gitlab_repo_url, branch, branches=True),
                f"ls_remote GitLab {package}/{branch}",
            ):
                return StringToolOutput(
                    result=f"Z-Stream branch {branch} already exists, no need to create it"
                )
        except Exception as e:
            raise ToolError(f"Failed to check GitLab remote: {sanitize_url(str(e))}") from e
        try:
            with tempfile.TemporaryDirectory() as path:
                # Username is taken from the Kerberos principal and embedded in
                # the URL explicitly — do not rely on the SSH config User setting.
                clone_url = f"ssh://{username}@pkgs.devel.redhat.com/rpms/{package}"
                clone_dest = os.path.join(path, package)

                async def _clone():
                    if os.path.exists(clone_dest):
                        shutil.rmtree(clone_dest)
                    return await asyncio.to_thread(git.Repo.clone_from, clone_url, clone_dest)

                repo = await _retry_transient(_clone, f"clone {package} from dist-git")
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
                    if await is_older_zstream(branch):
                        _, ref = await get_latest_z_pending_build(package, branch)
                    else:
                        _, ref = await get_latest_candidate_build(package, branch)
                    if source_branch := self._find_source_branch(repo, branch):
                        ref = await self._find_latest_same_nvr_ref(
                            repo,
                            package,
                            ref,
                            source_branch,
                        )
                    push_infos = await _retry_transient(
                        lambda: asyncio.to_thread(repo.remotes.origin.push, f"{ref}:refs/heads/{branch}"),
                        f"push {branch} to dist-git",
                    )
                    for info in push_infos:
                        if info.flags & git.remote.PushInfo.ERROR:
                            raise RuntimeError(f"Push rejected: {info.summary.strip()}")
                start_time = time.monotonic()
                while time.monotonic() - start_time < SYNC_TIMEOUT:
                    try:
                        if await asyncio.to_thread(
                            repo.git.ls_remote, gitlab_repo_url, branch, branches=True
                        ):
                            return StringToolOutput(result=f"Successfully created Z-Stream branch {branch}")
                    except git.exc.GitCommandError as e:
                        if not _is_transient_git_error(e):
                            raise
                        logger.warning(f"Transient error polling GitLab mirror sync: {sanitize_url(str(e))}")
                    elapsed = int(time.monotonic() - start_time)
                    logger.info(
                        f"Waiting for GitLab mirror sync of {package} branch {branch} ({elapsed}s elapsed)"
                    )
                    await asyncio.sleep(30)
                raise RuntimeError(
                    f"The {branch} branch wasn't synced to GitLab after {SYNC_TIMEOUT} seconds"
                )
        except Exception as e:
            raise ToolError(f"Failed to create Z-Stream branch: {sanitize_url(str(e))}") from e
