import asyncio
import logging
import os
import re
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

from ymir.common.base_utils import KerberosError, init_kerberos_ticket
from ymir.common.utils import get_latest_candidate_build
from ymir.tools.base import CloneableTool as Tool

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


def _sanitize_url(text: str) -> str:
    """Remove oauth2:{token}@ credentials from URLs in error messages."""
    return re.sub(r"oauth2:[^@\s]+@", "oauth2:***@", text)


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
                backoff = base_delay * 2**attempt
                logger.warning(
                    f"{label} failed (attempt {attempt + 1}/{max_retries}): "
                    f"{_sanitize_url(str(e))}; retrying in {backoff}s"
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
            raise ToolError(f"Failed to check GitLab remote: {_sanitize_url(str(e))}") from e
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
                    _, ref = await get_latest_candidate_build(package, branch)
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
                        logger.warning(f"Transient error polling GitLab mirror sync: {_sanitize_url(str(e))}")
                    elapsed = int(time.monotonic() - start_time)
                    logger.info(
                        f"Waiting for GitLab mirror sync of {package} branch {branch} ({elapsed}s elapsed)"
                    )
                    await asyncio.sleep(30)
                raise RuntimeError(
                    f"The {branch} branch wasn't synced to GitLab after {SYNC_TIMEOUT} seconds"
                )
        except Exception as e:
            raise ToolError(f"Failed to create Z-Stream branch: {_sanitize_url(str(e))}") from e
