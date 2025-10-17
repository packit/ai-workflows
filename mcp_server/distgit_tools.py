import asyncio
import os
import tempfile
import time
from typing import Annotated

import git
import koji
from fastmcp.exceptions import ToolError
from pydantic import Field

from common.constants import BREWHUB_URL
from common.utils import init_kerberos_ticket, KerberosError

SYNC_TIMEOUT = 1 * 60 * 60  # seconds


async def create_zstream_branch(
    package: Annotated[str, Field(description="Package name")],
    branch: Annotated[str, Field(description="Name of the branch to create")]
) -> str:
    """
    Creates a new Z-Stream branch for the specified package in internal dist-git.
    """
    try:
        principal = await init_kerberos_ticket()
    except KerberosError as e:
        raise ToolError(f"Failed to initialize Kerberos ticket: {e}") from e
    username = principal.split("@", maxsplit=1)[0]
    token = os.environ["GITLAB_TOKEN"]
    gitlab_repo_url = f"https://oauth2:{token}@gitlab.com/redhat/rhel/rpms/{package}"
    if await asyncio.to_thread(git.cmd.Git().ls_remote, gitlab_repo_url, branch, branches=True):
        return f"Z-Stream branch {branch} already exists, no need to create it"
    try:
        with tempfile.TemporaryDirectory() as path:
            repo = await asyncio.to_thread(
                git.Repo.clone_from,
                f"ssh://{username}@pkgs.devel.redhat.com/rpms/{package}",
                path,
            )
            if branch in [ref.name.split("/")[-1] for ref in repo.remotes.origin.refs]:
                raise RuntimeError(f"Z-Stream branch {branch} exists in dist-git but not on GitLab")
            # find the correct base for our new branch:
            # - get candidate tag corresponding to the branch
            # - get the latest build the candidate tag inherited (from Y-Stream or previous Z-Stream)
            # - get the ref the inherited build originated from
            # - use that as a base for the new branch
            # this allows us to create Z-Stream branches even if a higher Z-Stream branch already exists
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
            # wait until the new branch is synced to GitLab
            start_time = time.monotonic()
            while time.monotonic() - start_time < SYNC_TIMEOUT:
                if await asyncio.to_thread(repo.git.ls_remote, gitlab_repo_url, branch, branches=True):
                    return f"Successfully created Z-Stream branch {branch}"
                await asyncio.sleep(30)
            raise RuntimeError(f"The {branch} branch wasn't synced to GitLab after {SYNC_TIMEOUT} seconds")
    except Exception as e:
        raise ToolError(f"Failed to create Z-Stream branch: {e}") from e
