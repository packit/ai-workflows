import asyncio
import os
import re
import tempfile
import time

import git
import koji
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.common.constants import BREWHUB_URL
from ymir.common.utils import init_kerberos_ticket, KerberosError

SYNC_TIMEOUT = 1 * 60 * 60  # seconds


def _sanitize_url(text: str) -> str:
    """Remove oauth2:{token}@ credentials from URLs in error messages."""
    return re.sub(r"oauth2:[^@\s]+@", "oauth2:***@", text)


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
            if await asyncio.to_thread(git.cmd.Git().ls_remote, gitlab_repo_url, branch, branches=True):
                return StringToolOutput(result=f"Z-Stream branch {branch} already exists, no need to create it")
        except Exception as e:
            raise ToolError(f"Failed to check GitLab remote: {_sanitize_url(str(e))}") from e
        try:
            with tempfile.TemporaryDirectory() as path:
                repo = await asyncio.to_thread(
                    git.Repo.clone_from,
                    f"ssh://{username}@pkgs.devel.redhat.com/rpms/{package}",
                    path,
                )
                if branch in [ref.name.split("/")[-1] for ref in repo.remotes.origin.refs]:
                    raise RuntimeError(f"Z-Stream branch {branch} exists in dist-git but not on GitLab")
                session = koji.ClientSession(BREWHUB_URL)
                candidate_tag = f"{branch}-candidate"
                builds = await asyncio.to_thread(
                    session.listTagged,
                    package=package,
                    tag=candidate_tag,
                    latest=True,
                    inherit=True,
                    strict=True,
                )
                if not builds:
                    raise RuntimeError(f"There are no builds of {package} in {candidate_tag}")
                [build] = builds
                metadata = await asyncio.to_thread(session.getBuild, build["build_id"], strict=True)
                ref = metadata["source"].split("#")[-1]
                await asyncio.to_thread(repo.remotes.origin.push, f"{ref}:refs/heads/{branch}")
                start_time = time.monotonic()
                while time.monotonic() - start_time < SYNC_TIMEOUT:
                    if await asyncio.to_thread(repo.git.ls_remote, gitlab_repo_url, branch, branches=True):
                        return StringToolOutput(result=f"Successfully created Z-Stream branch {branch}")
                    await asyncio.sleep(30)
                raise RuntimeError(f"The {branch} branch wasn't synced to GitLab after {SYNC_TIMEOUT} seconds")
        except Exception as e:
            raise ToolError(f"Failed to create Z-Stream branch: {_sanitize_url(str(e))}") from e
