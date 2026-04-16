import asyncio
import json
import logging
import os
import re
from urllib.parse import quote, urlparse

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import (
    JSONToolOutput,
    StringToolOutput,
    Tool,
    ToolError,
    ToolRunOptions,
)
from ogr.exceptions import GitlabAPIException, OgrException
from ogr.factory import get_project
from ogr.services.gitlab.project import GitlabProject
from ogr.services.gitlab.pull_request import GitlabPullRequest
from pydantic import BaseModel, Field

from ymir.common.constants import AIOHTTP_TIMEOUT
from ymir.common.models import (
    CommentReply,
    FailedPipelineJob,
    MergeRequestComment,
    MergeRequestDetails,
    OpenMergeRequestResult,
)
from ymir.common.validators import AbsolutePath
from ymir.tools.privileged.utils import clean_stale_repositories

logger = logging.getLogger(__name__)

# GitLab access levels: Guest (10), Reporter (20), Developer (30),
# Maintainer (40), Owner (50)
DEVELOPER_ACCESS_LEVEL = 30

GITLAB_HOSTS = {"gitlab.com", "gitlab.cee.redhat.com"}

_GITLAB_COMMIT_RE = re.compile(r"^/(.+?)/-/commit/([0-9a-f]+)\.(?:patch|diff)$", re.IGNORECASE)


def _get_api_diff_url(url: str) -> str:
    """Convert a GitLab commit .patch/.diff web URL to an API diff URL.

    Returns the API URL for known GitLab hosts, or the original URL unchanged.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname not in GITLAB_HOSTS:
        return url

    match = _GITLAB_COMMIT_RE.match(parsed.path)
    if not match:
        return url

    project_path = match.group(1)
    sha = match.group(2)
    encoded_path = quote(project_path, safe="")
    return f"{parsed.scheme}://{hostname}/api/v4/projects/{encoded_path}/repository/commits/{sha}/diff"


def _get_auth_headers(url: str) -> dict[str, str]:
    """Return PRIVATE-TOKEN header if *url* points to a known GitLab host."""
    hostname = urlparse(url).hostname or ""
    if hostname in GITLAB_HOSTS:
        token = os.getenv("GITLAB_TOKEN")
        if token:
            return {"PRIVATE-TOKEN": token}
    return {}


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
    if not (
        match := re.search(
            r"gitlab\.com/([^/]+(?:/[^/]+){1,3})/-/merge_requests/(\d+)",
            merge_request_url,
        )
    ):
        raise ValueError(f"Could not parse merge request URL: {merge_request_url}")

    project_path = match.group(1)
    mr_id = int(match.group(2))

    project_url = f"https://gitlab.com/{project_path}"
    logger.info(f"Connecting to GitLab API for merge request: {project_url}")
    project = await asyncio.to_thread(get_project, url=project_url, token=os.getenv("GITLAB_TOKEN"))

    return await asyncio.to_thread(project.get_pr, mr_id)


async def _fetch_authorized_comments_from_merge_request_url(
    merge_request_url: str,
) -> list[MergeRequestComment]:
    mr = await _get_merge_request_from_url(merge_request_url)

    def get_authorized_comments():
        discussions = mr._raw_pr.discussions.list(get_all=True)

        authorized_member_ids = _get_authorized_member_ids(mr.target_project)

        authorized_comments = []
        for discussion in discussions:
            try:
                if not (notes := discussion.attributes.get("notes")):
                    continue

                first_note = notes[0]

                if first_note.get("system"):
                    continue

                author = first_note.get("author", {})
                author_id = author.get("id")
                if not author_id or author_id not in authorized_member_ids:
                    continue

                file_path, line_number, line_type = _extract_position_info(first_note)

                replies = [
                    reply
                    for note in notes[1:]
                    if (reply := _process_reply(authorized_member_ids, note)) is not None
                ]

                authorized_comments.append(
                    MergeRequestComment(
                        author=author.get("username"),
                        message=first_note.get("body"),
                        created_at=first_note.get("created_at"),
                        file_path=file_path,
                        line_number=line_number,
                        line_type=line_type,
                        discussion_id=getattr(discussion, "id", ""),
                        replies=replies,
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to process discussion: {e}")
                continue

        return authorized_comments

    return await asyncio.to_thread(get_authorized_comments)


class ForkRepositoryToolInput(BaseModel):
    repository: str = Field(description="Repository URL")


class ForkRepositoryTool(Tool[ForkRepositoryToolInput, ToolRunOptions, StringToolOutput]):
    name = "fork_repository"
    description = """
    Creates a new fork of the specified repository if it doesn't exist yet,
    otherwise gets the existing fork. Returns a clonable git URL of the fork.
    """
    input_schema = ForkRepositoryToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: ForkRepositoryToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        repository = tool_input.repository
        logger.info(f"Connecting to GitLab API to fork repository: {repository}")
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
            return StringToolOutput(result=fork.get_git_urls()["git"])

        def create_fork():
            prefix = "_".join(ns.replace("centos-stream", "centos") for ns in namespace[1:])
            fork_name = (f"{prefix}_" if prefix else "") + project.gitlab_repo.name
            fork = project.gitlab_repo.forks.create(data={"name": fork_name, "path": fork_name})
            return GitlabProject(
                namespace=fork.namespace["full_path"],
                service=project.service,
                repo=fork.path,
            )

        fork = await asyncio.to_thread(create_fork)
        if not fork:
            raise ToolError("Failed to fork the specified repository")
        return StringToolOutput(result=fork.get_git_urls()["git"])


class OpenMergeRequestToolInput(BaseModel):
    fork_url: str = Field(description="URL of the fork to open the MR from")
    title: str = Field(description="MR title")
    description: str = Field(description="MR description")
    target: str = Field(description="Target branch (in the original repository)")
    source: str = Field(description="Source branch (in the fork)")


class OpenMergeRequestTool(
    Tool[
        OpenMergeRequestToolInput,
        ToolRunOptions,
        JSONToolOutput[OpenMergeRequestResult],
    ]
):
    name = "open_merge_request"
    description = """
    Opens a new merge request from the specified fork against its original repository.

    Returns the merge request URL and whether the MR was newly created (False if an existing MR was reused).
    """
    input_schema = OpenMergeRequestToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: OpenMergeRequestToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[OpenMergeRequestResult]:
        fork_url = tool_input.fork_url
        title = tool_input.title
        description = tool_input.description
        target = tool_input.target
        source = tool_input.source
        logger.info(f"Connecting to GitLab API to open merge request from fork: {fork_url}")
        project = await asyncio.to_thread(get_project, url=fork_url, token=os.getenv("GITLAB_TOKEN"))
        if not project:
            raise ToolError("Failed to get the specified fork")
        is_new_mr = True
        try:
            pr = await asyncio.to_thread(project.create_pr, title, description, target, source)
        except GitlabAPIException as ex:
            logger.info("Gitlab API exception: %s", ex)
            if ex.response_code == 409:
                prs = await asyncio.to_thread(project.parent.get_pr_list)
                for pr in prs:
                    if pr.source_branch == source and pr.target_branch == target:
                        logger.info("Reusing existing MR %s", pr)
                        pr.description = description
                        pr.title = title
                        is_new_mr = False
                        break
                else:
                    raise
            else:
                raise
        if not pr:
            raise ToolError("Failed to open the merge request")

        return JSONToolOutput(result=OpenMergeRequestResult(url=pr.url, is_new_mr=is_new_mr))


class GetInternalRhelBranchesToolInput(BaseModel):
    package: str = Field(description="Package name to check branches for")


class GetInternalRhelBranchesTool(
    Tool[GetInternalRhelBranchesToolInput, ToolRunOptions, JSONToolOutput[list[str]]]
):
    name = "get_internal_rhel_branches"
    description = """
    Gets the list of branches in the internal RHEL dist-git repository for the specified package.
    Returns a list of branch names.
    """
    input_schema = GetInternalRhelBranchesToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetInternalRhelBranchesToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[str]]:
        package = tool_input.package
        repository_url = f"https://gitlab.com/redhat/rhel/rpms/{package}"
        logger.info(f"Connecting to GitLab API to get branches for package: {repository_url}")

        try:
            project = await asyncio.to_thread(
                get_project, url=repository_url, token=os.getenv("GITLAB_TOKEN")
            )
            if not project:
                raise ToolError(f"Failed to get repository for package: {package}")

            branches = await asyncio.to_thread(project.get_branches)
            logger.info(f"Found {len(branches)} branches for package {package}: {branches}")
            return JSONToolOutput(result=branches)

        except OgrException as ex:
            logger.warning(f"Failed to get branches for package {package}: {ex}")
            raise ToolError(f"Failed to get branches for package {package}: {ex}") from ex


class CloneRepositoryToolInput(BaseModel):
    repository: str = Field(description="Repository to clone")
    branch: str = Field(description="Branch to clone")
    clone_path: AbsolutePath = Field(description="Absolute path where to clone the repository")


class CloneRepositoryTool(Tool[CloneRepositoryToolInput, ToolRunOptions, StringToolOutput]):
    name = "clone_repository"
    description = """
    Clones the specified repository to the given local path.
    """
    input_schema = CloneRepositoryToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: CloneRepositoryToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        repository = tool_input.repository
        branch = tool_input.branch
        clone_path = tool_input.clone_path
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

        return StringToolOutput(result=f"Successfully cloned the specified repository to {clone_path}")


class PushToRemoteRepositoryToolInput(BaseModel):
    repository: str = Field(description="Repository URL")
    clone_path: AbsolutePath = Field(description="Absolute path to local clone of the repository")
    branch: str = Field(description="Branch to push")
    force: bool = Field(default=False, description="Whether to overwrite the remote ref")


class PushToRemoteRepositoryTool(Tool[PushToRemoteRepositoryToolInput, ToolRunOptions, StringToolOutput]):
    name = "push_to_remote_repository"
    description = """
    Pushes the specified branch from a local clone to the specified remote repository.
    """
    input_schema = PushToRemoteRepositoryToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: PushToRemoteRepositoryToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        repository = tool_input.repository
        clone_path = tool_input.clone_path
        branch = tool_input.branch
        force = tool_input.force
        remote = _get_authenticated_url(repository)
        command = ["git", "push", remote, branch]
        if force:
            command.append("--force")
        proc = await asyncio.create_subprocess_exec(command[0], *command[1:], cwd=clone_path)
        if await proc.wait():
            raise ToolError("Failed to push to the specified repository")
        return StringToolOutput(result=f"Successfully pushed the specified branch to {repository}")


class AddMergeRequestLabelsToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")
    labels: list[str] = Field(description="List of labels to add to the merge request")


class AddMergeRequestLabelsTool(Tool[AddMergeRequestLabelsToolInput, ToolRunOptions, StringToolOutput]):
    name = "add_merge_request_labels"
    description = """
    Adds labels to an existing merge request.
    """
    input_schema = AddMergeRequestLabelsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: AddMergeRequestLabelsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        merge_request_url = tool_input.merge_request_url
        labels = tool_input.labels
        try:
            mr = await _get_merge_request_from_url(merge_request_url)
            for label in labels:
                await asyncio.to_thread(mr.add_label, label)
            return StringToolOutput(
                result=f"Successfully added labels {labels} to merge request {merge_request_url}"
            )
        except Exception as e:
            raise ToolError(f"Failed to add labels to merge request: {e}") from e


class AddMergeRequestCommentToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")
    comment: str = Field(description="Comment text")


class AddMergeRequestCommentTool(Tool[AddMergeRequestCommentToolInput, ToolRunOptions, StringToolOutput]):
    name = "add_merge_request_comment"
    description = """
    Adds a comment to an existing merge request.
    """
    input_schema = AddMergeRequestCommentToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: AddMergeRequestCommentToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        merge_request_url = tool_input.merge_request_url
        comment = tool_input.comment
        try:
            mr = await _get_merge_request_from_url(merge_request_url)
            await asyncio.to_thread(mr._raw_pr.notes.create, {"body": comment})
            return StringToolOutput(result=f"Successfully added comment to merge request {merge_request_url}")
        except Exception as e:
            raise ToolError(f"Failed to add comment to merge request: {e}") from e


class AddBlockingMergeRequestCommentToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")
    comment: str = Field(description="Comment text to add as a blocking discussion")


class AddBlockingMergeRequestCommentTool(
    Tool[AddBlockingMergeRequestCommentToolInput, ToolRunOptions, StringToolOutput]
):
    name = "add_blocking_merge_request_comment"
    description = """
    Adds a blocking (unresolved) comment/discussion to an existing merge request.
    This will block the MR from being merged until the discussion is resolved.
    Checks if the exact same comment already exists (resolved or unresolved) before adding.
    """
    input_schema = AddBlockingMergeRequestCommentToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: AddBlockingMergeRequestCommentToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        merge_request_url = tool_input.merge_request_url
        comment = tool_input.comment
        try:
            mr = await _get_merge_request_from_url(merge_request_url)

            def check_existing_comment():
                discussions = mr._raw_pr.discussions.list(get_all=True)

                blocking_comment_message = comment.strip()

                for discussion in discussions:
                    notes = discussion.attributes.get("notes", [])
                    if notes and notes[0].get("body", "").strip() == blocking_comment_message:
                        return True

                return False

            exists = await asyncio.to_thread(check_existing_comment)
            if exists:
                return StringToolOutput(
                    result=f"Comment already exists in merge request "
                    f"{merge_request_url}, not adding duplicate"
                )

            await asyncio.to_thread(
                mr._raw_pr.discussions.create,
                {"body": comment},
            )

            return StringToolOutput(
                result=f"Successfully added blocking comment to merge request {merge_request_url}"
            )
        except Exception as e:
            raise ToolError(f"Failed to add blocking comment to merge request: {e}") from e


class RetryPipelineJobToolInput(BaseModel):
    project_url: str = Field(description="GitLab project URL")
    job_id: int = Field(description="Job ID to retry")


class RetryPipelineJobTool(Tool[RetryPipelineJobToolInput, ToolRunOptions, StringToolOutput]):
    name = "retry_pipeline_job"
    description = """
    Retries a specific job in a GitLab pipeline.
    """
    input_schema = RetryPipelineJobToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: RetryPipelineJobToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        project_url = tool_input.project_url
        job_id = tool_input.job_id
        logger.info(f"Connecting to GitLab API to retry job {job_id} for project: {project_url}")
        try:
            project = await asyncio.to_thread(get_project, url=project_url, token=os.getenv("GITLAB_TOKEN"))

            def retry_gitlab_job():
                job = project.gitlab_repo.jobs.get(job_id)
                job.retry()
                return job

            job = await asyncio.to_thread(retry_gitlab_job)

            logger.info(f"Successfully retried job {job_id} for project {project_url}")
            return StringToolOutput(result=f"Successfully retried job {job_id}. Status: {job.status}")

        except Exception as e:
            logger.error(f"Failed to retry job {job_id} for project {project_url}: {e}")
            raise ToolError(f"Failed to retry job: {e}") from e


class GetFailedPipelineJobsFromMergeRequestToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")


class GetFailedPipelineJobsFromMergeRequestTool(
    Tool[
        GetFailedPipelineJobsFromMergeRequestToolInput,
        ToolRunOptions,
        JSONToolOutput[list[FailedPipelineJob]],
    ]
):
    name = "get_failed_pipeline_jobs_from_merge_request"
    description = """
    Gets the failed pipeline jobs from the latest pipeline of a merge request.
    Returns a list of failed pipeline jobs with their details.
    """
    input_schema = GetFailedPipelineJobsFromMergeRequestToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetFailedPipelineJobsFromMergeRequestToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[FailedPipelineJob]]:
        merge_request_url = tool_input.merge_request_url
        try:
            mr = await _get_merge_request_from_url(merge_request_url)

            def get_latest_pipeline_jobs():
                if not hasattr(mr._raw_pr, "head_pipeline") or not mr._raw_pr.head_pipeline:
                    return []

                pipeline_id = mr._raw_pr.head_pipeline["id"]
                pipeline = mr.target_project.gitlab_repo.pipelines.get(pipeline_id)
                jobs = pipeline.jobs.list(get_all=True)

                namespace = mr.target_project.namespace
                repo = mr.target_project.repo
                return [
                    FailedPipelineJob(
                        id=str(job.id),
                        name=job.name,
                        url=f"https://gitlab.com/{namespace}/{repo}/-/jobs/{job.id}",
                        status=job.status,
                        stage=job.stage,
                        artifacts_url=(
                            f"https://gitlab.com/{namespace}/{repo}/-/jobs/{job.id}/artifacts/browse"
                            if hasattr(job, "artifacts_file") and job.artifacts_file
                            else ""
                        ),
                    )
                    for job in jobs
                    if job.status == "failed"
                ]

            failed_jobs = await asyncio.to_thread(get_latest_pipeline_jobs)

            logger.info(f"Found {len(failed_jobs)} failed jobs in latest pipeline for MR {merge_request_url}")
            return JSONToolOutput(result=failed_jobs)

        except Exception as e:
            logger.error(f"Failed to get failed jobs from MR {merge_request_url}: {e}")
            raise ToolError(f"Failed to get failed jobs from merge request: {e}") from e


def _get_authorized_member_ids(project: GitlabProject) -> set[int]:
    """
    Fetch all project members and return a set of IDs for members
    with Developer role or higher. This avoids N+1 API calls.
    """
    try:
        members = project.gitlab_repo.members_all.list(get_all=True)
        return {member.id for member in members if member.access_level >= DEVELOPER_ACCESS_LEVEL}
    except Exception as e:
        logger.warning(f"Failed to fetch project members: {e}")
        return set()


def _extract_position_info(note: dict) -> tuple[str, int | None, str]:
    """Extract file path, line number, and line type from a note's position."""
    if not (position := note.get("position")):
        return "", None, ""

    file_path = position.get("new_path", "") or position.get("old_path", "")
    new_line = position.get("new_line")
    old_line = position.get("old_line")

    if new_line and old_line:
        return file_path, new_line, "unchanged"
    if new_line:
        return file_path, new_line, "new"
    if old_line:
        return file_path, old_line, "old"

    return file_path, None, ""


def _process_reply(authorized_member_ids: set[int], note: dict) -> CommentReply | None:
    """Process a reply note and return CommentReply if author is authorized."""
    if note.get("system", False):
        return None

    try:
        author = note.get("author", {})
        author_id = author.get("id")
        if not author_id or author_id not in authorized_member_ids:
            return None

        return CommentReply(
            author=author.get("username"),
            message=note.get("body"),
            created_at=note.get("created_at"),
        )
    except Exception as e:
        logger.warning(f"Failed to process reply note: {e}")
        return None


class GetAuthorizedCommentsFromMergeRequestToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")


class GetAuthorizedCommentsFromMergeRequestTool(
    Tool[
        GetAuthorizedCommentsFromMergeRequestToolInput,
        ToolRunOptions,
        JSONToolOutput[list[MergeRequestComment]],
    ]
):
    name = "get_authorized_comments_from_merge_request"
    description = """
    Gets all comments from a merge request, filtered to only include
    comments from authorized members with Developer role or higher.
    """
    input_schema = GetAuthorizedCommentsFromMergeRequestToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetAuthorizedCommentsFromMergeRequestToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[MergeRequestComment]]:
        merge_request_url = tool_input.merge_request_url
        try:
            comments = await _fetch_authorized_comments_from_merge_request_url(merge_request_url)
            return JSONToolOutput(result=comments)
        except Exception as e:
            raise ToolError(f"Failed to get authorized comments from merge request: {e}") from e


class GetMergeRequestDetailsToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")


class GetMergeRequestDetailsTool(
    Tool[
        GetMergeRequestDetailsToolInput,
        ToolRunOptions,
        JSONToolOutput[MergeRequestDetails],
    ]
):
    name = "get_merge_request_details"
    description = """
    Retrieves details about the specified merge request.
    """
    input_schema = GetMergeRequestDetailsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GetMergeRequestDetailsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[MergeRequestDetails]:
        merge_request_url = tool_input.merge_request_url
        try:
            mr = await _get_merge_request_from_url(merge_request_url)
            comments = await _fetch_authorized_comments_from_merge_request_url(merge_request_url)
            username = mr.source_project.service.user.get_username()
            return JSONToolOutput(
                result=MergeRequestDetails(
                    source_repo=mr.source_project.get_git_urls()["git"],
                    source_branch=mr.source_branch,
                    target_repo_name=mr.target_project.gitlab_repo.name,
                    target_branch=mr.target_branch,
                    title=mr.title,
                    description=mr.description,
                    last_updated_at=mr._raw_pr.updated_at,
                    comments=[c for c in comments if f"@{username}" in c.message],
                )
            )
        except Exception as e:
            raise ToolError(f"Failed to get merge request details: {e}") from e


MAX_PATCH_CONTENT_LENGTH = 2000


class GetPatchFromUrlToolInput(BaseModel):
    patch_url: str = Field(description="URL to a patch or diff file")


class GetPatchFromUrlTool(Tool[GetPatchFromUrlToolInput, ToolRunOptions, StringToolOutput]):
    name = "get_patch_from_url"
    description = """
    Fetches a patch/diff from a URL.
    Returns the patch content as text (truncated to the first 2000 characters for large patches).
    """
    input_schema = GetPatchFromUrlToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    @staticmethod
    def _truncate(text: str, max_length: int = MAX_PATCH_CONTENT_LENGTH) -> str:
        if len(text) <= max_length:
            return text
        return (
            text[:max_length]
            + f"\n\n[Content truncated - showing first {max_length} characters of {len(text)} total]"
        )

    async def _run(
        self,
        tool_input: GetPatchFromUrlToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        patch_url = tool_input.patch_url
        request_url = _get_api_diff_url(patch_url)
        headers = _get_auth_headers(request_url)

        try:
            async with (
                aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                session.get(request_url, headers=headers) as response,
            ):
                if response.status >= 400:
                    raise ToolError(f"Failed to fetch patch from {patch_url}: HTTP {response.status}")
                text = await response.text()
        except aiohttp.ClientError as e:
            raise ToolError(f"Failed to fetch patch from {patch_url}: {e}") from e
        return StringToolOutput(result=self._truncate(text))


class FetchGitlabMrNotesInput(BaseModel):
    project: str = Field(description="GitLab project path (e.g. 'redhat/centos-stream/rpms/podman')")
    mr_iid: int = Field(description="Merge request IID within the project")


class FetchGitlabMrNotesTool(Tool[FetchGitlabMrNotesInput, ToolRunOptions, StringToolOutput]):
    """
    Tool to fetch comments/notes from a GitLab merge request.
    This is useful for finding OSCI test results posted as comments
    on merge requests with titles like "Results for pipeline ...".
    """

    name = "fetch_gitlab_mr_notes"  # type: ignore
    description = (  # type: ignore
        "Fetch comments/notes from a GitLab merge request. "
        "Returns JSON with a list of notes including author, body, and creation date. "
        "Use this to find OSCI test results posted as comments on merge requests."
    )
    input_schema = FetchGitlabMrNotesInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: FetchGitlabMrNotesInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        encoded_project = quote(input.project, safe="")
        url = f"https://gitlab.com/api/v4/projects/{encoded_project}/merge_requests/{input.mr_iid}/notes"
        headers = _get_auth_headers(url)
        logger.info("Fetching MR notes from %s", url)

        try:
            async with (
                aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                session.get(
                    url,
                    headers=headers,
                    params={
                        "per_page": "100",
                        "sort": "desc",
                        "order_by": "created_at",
                    },
                ) as response,
            ):
                if response.status != 200:
                    text = await response.text()
                    logger.error(
                        "Failed to fetch MR notes (HTTP %d): %s",
                        response.status,
                        text,
                    )
                    return StringToolOutput(
                        result=f"Failed to fetch notes for MR !{input.mr_iid} "
                        f"in {input.project} (HTTP {response.status}): {text}"
                    )

                notes = await response.json()

            result = [
                {
                    "author": note.get("author", {}).get("name", "Unknown"),
                    "body": note["body"],
                    "created_at": note.get("created_at"),
                    "system": note.get("system", False),
                }
                for note in notes
            ]

            return StringToolOutput(result=json.dumps(result, indent=2))

        except Exception as e:
            logger.error("Error fetching GitLab MR notes: %s", e)
            return StringToolOutput(result=f"Error fetching GitLab MR notes: {e}")
