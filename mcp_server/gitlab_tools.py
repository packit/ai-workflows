import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError
from ogr.factory import get_project
from ogr.exceptions import OgrException, GitlabAPIException
from ogr.services.gitlab.project import GitlabProject
from pydantic import Field

from common.validators import AbsolutePath


logger = logging.getLogger(__name__)


async def fork_repository(
    repository: Annotated[str, Field(description="Repository URL")],
) -> str:
    """
    Creates a new fork of the specified repository if it doesn't exist yet,
    otherwise gets the existing fork. Returns a clonable git URL of the fork.
    """
    project = await asyncio.to_thread(get_project, url=repository, token=os.getenv("GITLAB_TOKEN"))
    if not project:
        raise ToolError("Failed to get the specified repository")

    if urlparse(project.service.instance_url).hostname != "gitlab.com":
        raise ToolError("Unexpected git forge, expected gitlab.com/redhat")

    namespace = project.gitlab_repo.namespace["full_path"].split("/")
    if not namespace or namespace[0] != "redhat":
        raise ToolError("Unexpected GitLab project, expected gitlab.com/redhat")

    def get_fork():
        username = project.service.user.get_username()
        for fork in project.get_forks():
            if fork.gitlab_repo.namespace["full_path"] == username:
                return fork
        return None

    if fork := await asyncio.to_thread(get_fork):
        return fork.get_git_urls()["git"]

    def create_fork():
        # follow the convention set by `centpkg fork` and prefix repo name with namespace, e.g.:
        # * gitlab.com/redhat/centos-stream/rpms/bash => gitlab.com/jotnar-bot/centos_rpms_bash
        # * gitlab.com/redhat/rhel/rpms/bash => gitlab.com/jotnar-bot/rhel_rpms_bash
        prefix = "_".join(ns.replace("centos-stream", "centos") for ns in namespace[1:])
        fork_name = (f"{prefix}_" if prefix else "") + project.gitlab_repo.name
        fork = project.gitlab_repo.forks.create(data={"name": fork_name, "path": fork_name})
        return GitlabProject(namespace=fork.namespace["full_path"], service=project.service, repo=fork.path)

    fork = await asyncio.to_thread(create_fork)
    if not fork:
        raise ToolError("Failed to fork the specified repository")
    return fork.get_git_urls()["git"]


async def open_merge_request(
    fork_url: Annotated[str, Field(description="URL of the fork to open the MR from")],
    title: Annotated[str, Field(description="MR title")],
    description: Annotated[str, Field(description="MR description")],
    target: Annotated[str, Field(description="Target branch (in the original repository)")],
    source: Annotated[str, Field(description="Source branch (in the fork)")],
) -> str:
    """
    Opens a new merge request from the specified fork against its original repository.
    Returns URL of the opened merge request.
    """
    project = await asyncio.to_thread(get_project, url=fork_url, token=os.getenv("GITLAB_TOKEN"))
    if not project:
        raise ToolError("Failed to get the specified fork")
    try:
        pr = await asyncio.to_thread(project.create_pr, title, description, target, source)
    except GitlabAPIException as ex:
        logger.info("Gitlab API exception: %s", ex)
        if ex.response_code == 409:
            # 409 code means conflict: MR already exists; let's verify
            prs = await asyncio.to_thread(project.parent.get_pr_list)
            for pr in prs:
                if pr.source_branch == source and pr.target_branch == target:
                    logger.info("Reusing existing MR %s", pr)
                    # we have to update the MR description to include the new commit hash
                    # this is an active API call via PR's setter method
                    pr.description = description
                    pr.title = title
                    break
            else:
                raise
        else:
            raise
    if not pr:
        raise ToolError("Failed to open the merge request")

    for attempt in range(5):
        try:
            # First, verify the MR exists before trying to add the label
            pr = await asyncio.to_thread(project.parent.get_pr, pr.id)
            # by default, set this label on a newly created MR so we can inspect it ASAP
            await asyncio.to_thread(pr.add_label, "jotnar_needs_attention")
            break
        except OgrException as ex:
            logger.info("Failed to add label on attempt %d/5, retrying. Error: %s", attempt + 1, ex)
            await asyncio.sleep(0.5 * (2 ** attempt))
    else:
        logger.error("MR %s does not appear to exist after creation", pr)
        logger.error("Unable to set label 'jotnar_needs_attention' on the MR")
    return pr.url


async def push_to_remote_repository(
    repository: Annotated[str, Field(description="Repository URL")],
    clone_path: Annotated[AbsolutePath, Field(description="Absolute path to local clone of the repository")],
    branch: Annotated[str, Field(description="Branch to push")],
    force: Annotated[bool, Field(description="Whether to overwrite the remote ref")] = False,
) -> str:
    """
    Pushes the specified branch from a local clone to the specified remote repository.
    """
    url = urlparse(repository)
    token = os.getenv("GITLAB_TOKEN")
    remote = url._replace(netloc=f"oauth2:{token}@{url.hostname}").geturl()
    command = ["git", "push", remote, branch]
    if force:
        command.append("--force")
    proc = await asyncio.create_subprocess_exec(command[0], *command[1:], cwd=clone_path)
    if await proc.wait():
        raise ToolError("Failed to push to the specified repository")
    return f"Successfully pushed the specified branch to {repository}"
