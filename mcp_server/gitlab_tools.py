import asyncio
import logging
import os
import re
from typing import Annotated, Tuple
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError
from ogr.factory import get_project
from ogr.exceptions import OgrException, GitlabAPIException
from ogr.services.gitlab.project import GitlabProject
from ogr.services.gitlab.pull_request import GitlabPullRequest
from pydantic import Field

from common.validators import AbsolutePath
from utils import clean_stale_repositories


logger = logging.getLogger(__name__)


def _get_authenticated_url(repository_url: str) -> str:
    """
    Helper function to add GitLab token authentication to repository URLs.
    """
    if token := os.getenv("GITLAB_TOKEN"):
        url = urlparse(repository_url)
        return url._replace(netloc=f"oauth2:{token}@{url.hostname}").geturl()
    return repository_url


async def _get_merge_request_from_url(merge_request_url: str) -> GitlabPullRequest:
    """
    Helper function to parse a merge request URL and return the MR object.

    Returns:
        The GitLab merge request (PullRequest) object
    """
    # Extract project and MR ID from the URL
    # URL format examples:
    # `https://gitlab.com/namespace/project/-/merge_requests/123`
    # `https://gitlab.com/redhat/rhel/rpms/package/-/merge_requests/123`
    if not (match := re.search(r'gitlab\.com/([^/]+(?:/[^/]+){1,3})/-/merge_requests/(\d+)', merge_request_url)):
        raise ValueError(f"Could not parse merge request URL: {merge_request_url}")

    project_path = match.group(1)
    mr_id = int(match.group(2))

    project_url = f"https://gitlab.com/{project_path}"
    project = await asyncio.to_thread(
        get_project, url=project_url, token=os.getenv("GITLAB_TOKEN")
    )

    return await asyncio.to_thread(project.get_pr, mr_id)


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
) -> Tuple[str, bool]:
    """
    Opens a new merge request from the specified fork against its original repository.

    Returns:
        - str: The URL of the merge request if it was created successfully
        - bool: True if the merge request was created, False otherwise (i.e. MR was reused)
    """
    project = await asyncio.to_thread(get_project, url=fork_url, token=os.getenv("GITLAB_TOKEN"))
    if not project:
        raise ToolError("Failed to get the specified fork")
    is_brand_new_mr = True
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
                    is_brand_new_mr = False
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
            # It can take some time for the MR to be available via API
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
    return pr.url, is_brand_new_mr


async def get_internal_rhel_branches(
    package: Annotated[str, Field(description="Package name to check branches for")],
) -> list[str]:
    """
    Gets the list of branches in the internal RHEL dist-git repository for the specified package.
    Returns a list of branch names.
    """
    repository_url = f"https://gitlab.com/redhat/rhel/rpms/{package}"

    try:
        project = await asyncio.to_thread(get_project, url=repository_url, token=os.getenv("GITLAB_TOKEN"))
        if not project:
            raise ToolError(f"Failed to get repository for package: {package}")

        branches = await asyncio.to_thread(project.get_branches)
        logger.info(f"Found {len(branches)} branches for package {package}: {branches}")
        return branches

    except OgrException as ex:
        logger.warning(f"Failed to get branches for package {package}: {ex}")
        raise ToolError(f"Failed to get branches for package {package}: {ex}")


async def clone_repository(
    repository: Annotated[str, Field(description="Repository to clone")],
    branch: Annotated[str, Field(description="Branch to clone")],
    clone_path: Annotated[AbsolutePath, Field(description="Absolute path where to clone the repository")],
) -> str:
    """
    Clones the specified repository to the given local path.
    """
    # Clean up old repositories before cloning
    await clean_stale_repositories()

    clone_path.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec("git", "init", cwd=clone_path)
    if await proc.wait():
        raise ToolError(f"Failed to initialize git repo at {clone_path}")

    command = [
        "git",
        "fetch",
        _get_authenticated_url(repository),
        f"{branch}:refs/heads/{branch}",
    ]

    proc = await asyncio.create_subprocess_exec(command[0], *command[1:], cwd=clone_path)
    if await proc.wait():
        raise ToolError(f"Failed to fetch {branch} from {repository}")

    command = [
        "git",
        "checkout",
        branch,
    ]

    proc = await asyncio.create_subprocess_exec(command[0], *command[1:], cwd=clone_path)
    if await proc.wait():
        raise ToolError(f"Failed to checkout branch {branch}")

    return f"Successfully cloned the specified repository to {clone_path}"


async def push_to_remote_repository(
    repository: Annotated[str, Field(description="Repository URL")],
    clone_path: Annotated[AbsolutePath, Field(description="Absolute path to local clone of the repository")],
    branch: Annotated[str, Field(description="Branch to push")],
    force: Annotated[bool, Field(description="Whether to overwrite the remote ref")] = False,
) -> str:
    """
    Pushes the specified branch from a local clone to the specified remote repository.
    """
    remote = _get_authenticated_url(repository)
    command = ["git", "push", remote, branch]
    if force:
        command.append("--force")
    proc = await asyncio.create_subprocess_exec(command[0], *command[1:], cwd=clone_path)
    if await proc.wait():
        raise ToolError("Failed to push to the specified repository")
    return f"Successfully pushed the specified branch to {repository}"


async def add_merge_request_labels(
    merge_request_url: Annotated[str, Field(description="URL of the merge request")],
    labels: Annotated[list[str], Field(description="List of labels to add to the merge request")],
) -> str:
    """
    Adds labels to an existing merge request.
    """
    try:
        mr = await _get_merge_request_from_url(merge_request_url)
        for label in labels:
            await asyncio.to_thread(mr.add_label, label)
        return f"Successfully added labels {labels} to merge request {merge_request_url}"
    except Exception as e:
        raise ToolError(f"Failed to add labels to merge request: {e}")


async def add_blocking_merge_request_comment(
    merge_request_url: Annotated[str, Field(description="URL of the merge request")],
    comment: Annotated[str, Field(description="Comment text to add as a blocking discussion")],
) -> str:
    """
    Adds a blocking (unresolved) comment/discussion to an existing merge request.
    This will block the MR from being merged until the discussion is resolved.
    """
    try:
        mr = await _get_merge_request_from_url(merge_request_url)
        # Discussions are created unresolved by default, which blocks the MR
        await asyncio.to_thread(
            mr._raw_pr.discussions.create,
            {"body": comment},
        )

        return f"Successfully added blocking comment to merge request {merge_request_url}"
    except Exception as e:
        raise ToolError(f"Failed to add blocking comment to merge request: {e}")


async def create_merge_request_checklist(
    merge_request_url: Annotated[str, Field(description="URL of the merge request")],
    note_body: Annotated[str, Field(description="Body of the note to create")],
) -> str:
    """
    Creates our pre/post merge checklist for our dist-git merge requests.
    """
    try:
        mr = await _get_merge_request_from_url(merge_request_url)
        # internal note docs: https://docs.gitlab.com/api/notes/#create-new-issue-note
        await asyncio.to_thread(mr._raw_pr.notes.create, {"body": note_body}, internal=True)
        return f"Successfully created checklist for merge request {merge_request_url}"
    except Exception as e:
        raise ToolError(f"Failed to create checklist for merge request: {e}")


async def retry_pipeline_job(
    project_url: Annotated[str, Field(description="GitLab project URL")],
    job_id: Annotated[int, Field(description="Job ID to retry")],
) -> str:
    """
    Retries a specific job in a GitLab pipeline.
    """
    try:
        project = await asyncio.to_thread(
            get_project, url=project_url, token=os.getenv("GITLAB_TOKEN")
        )

        def retry_gitlab_job():
            job = project.gitlab_repo.jobs.get(job_id)
            job.retry()
            return job

        job = await asyncio.to_thread(retry_gitlab_job)

        logger.info(f"Successfully retried job {job_id} for project {project_url}")
        return f"Successfully retried job {job_id}. Status: {job.status}"

    except Exception as e:
        logger.error(f"Failed to retry job {job_id} for project {project_url}: {e}")
        raise ToolError(f"Failed to retry job: {e}") from e


async def get_failed_pipeline_jobs_from_merge_request(
    merge_request_url: Annotated[str, Field(description="URL of the merge request")],
) -> list[dict[str, str]]:
    """
    Gets the failed pipeline jobs from the latest pipeline of a merge request.
    Returns a list of failed jobs with their details (id, name, url, status).
    """
    try:
        mr = await _get_merge_request_from_url(merge_request_url)

        def get_latest_pipeline_jobs():
            # Use head_pipeline to get the latest pipeline for this MR
            if not hasattr(mr._raw_pr, "head_pipeline") or not mr._raw_pr.head_pipeline:
                return []

            pipeline_id = mr._raw_pr.head_pipeline["id"]
            pipeline = mr.target_project.gitlab_repo.pipelines.get(pipeline_id)
            jobs = pipeline.jobs.list(get_all=True)

            namespace = mr.target_project.namespace
            repo = mr.target_project.repo
            failed_jobs = [
                {
                    "id": str(job.id),
                    "name": job.name,
                    "url": f"https://gitlab.com/{namespace}/{repo}/-/jobs/{job.id}",
                    "status": job.status,
                    "stage": job.stage,
                    "artifacts_url": (
                        f"https://gitlab.com/{namespace}/{repo}/-/jobs/{job.id}/artifacts/browse"
                        if hasattr(job, "artifacts_file") and job.artifacts_file
                        else ""
                    ),
                }
                for job in jobs
                if job.status == "failed"
            ]

            return failed_jobs

        failed_jobs = await asyncio.to_thread(get_latest_pipeline_jobs)

        logger.info(f"Found {len(failed_jobs)} failed jobs in latest pipeline for MR {merge_request_url}")
        return failed_jobs

    except Exception as e:
        logger.error(f"Failed to get failed jobs from MR {merge_request_url}: {e}")
        raise ToolError(f"Failed to get failed jobs from merge request: {e}") from e
