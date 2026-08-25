import asyncio
import base64
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
import gitlab
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import (
    JSONToolOutput,
    StringToolOutput,
    ToolError,
    ToolRunOptions,
)
from mcp.server.lowlevel.server import request_ctx
from ogr.exceptions import GitlabAPIException, OgrException
from ogr.factory import get_project
from ogr.services.gitlab.project import GitlabProject
from ogr.services.gitlab.pull_request import GitlabPullRequest
from pydantic import BaseModel, Field

from ymir.common.base_utils import run_subprocess
from ymir.common.models import (
    CommentReply,
    FailedPipelineJob,
    MergeRequestComment,
    MergeRequestDetails,
    OpenMergeRequestResult,
)
from ymir.common.validators import AbsolutePath
from ymir.tools.base import CloneableTool as Tool
from ymir.tools.constants import AIOHTTP_TIMEOUT, YMIR_USER_AGENT
from ymir.tools.http import aiohttp_get_with_retries
from ymir.tools.privileged.utils import clean_stale_repositories, sanitize_url

logger = logging.getLogger(__name__)

_STDERR_HINT_MAX = 500


def _git_subprocess_error(stderr: str | None, message: str) -> ToolError:
    """Log a git subprocess failure at ERROR level and return a ToolError with a stderr hint."""
    safe_stderr = sanitize_url(_sanitize_git_stderr(stderr or ""))
    hint = safe_stderr.strip()[-_STDERR_HINT_MAX:]
    logger.error("%s\nstderr (last %d chars): %s", message, len(hint), hint)
    return ToolError(f"{message}: {hint}" if hint else message)


async def _run_git_cmd(
    command: list[str],
    *,
    label: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = 3600,
) -> None:
    """Run a git subprocess with structured logging, timing, and error handling.

    Args:
        command: The git command to execute as a list of arguments.
        label: Human-readable description for log messages (e.g. "git fetch <url> branch=main").
        cwd: Working directory for the subprocess.
        env: Environment variables to pass to the subprocess.
        timeout: Timeout in seconds, or None for no timeout.

    Raises:
        ToolError: If the command fails or times out.
    """
    logger.info("%s", label)
    t0 = time.monotonic()
    try:
        coro = run_subprocess(command, cwd=cwd, env=env)
        if timeout is not None:
            returncode, _, stderr = await asyncio.wait_for(coro, timeout=timeout)
        else:
            returncode, _, stderr = await coro
    except TimeoutError:
        elapsed = time.monotonic() - t0
        logger.error("%s timed out after %.1fs", label, elapsed)
        raise ToolError(f"{label} timed out after {int(timeout)}s") from None
    elapsed = time.monotonic() - t0
    if returncode:
        raise _git_subprocess_error(
            stderr, f"{label} failed (exit_code={returncode}, elapsed={elapsed:.1f}s)"
        )
    logger.info("%s completed in %.1fs", label, elapsed)


# GitLab access levels: Guest (10), Reporter (20), Developer (30),
# Maintainer (40), Owner (50)
DEVELOPER_ACCESS_LEVEL = 30

_FORK_READY_IMPORT_STATUSES = frozenset({"finished", "none"})
_FORK_READY_POLL_INTERVAL_SEC = 2.0
_FORK_READY_TIMEOUT_SEC = 110  # leave margin under fork_repository tool timeout


def _fork_api_project(fork: GitlabProject):
    """Return a python-gitlab Project object suitable for import_status polling.

    ``forks.create()`` returns a ``ProjectFork`` without ``refresh()``; always
    fetch the full project by path for status polling.
    """
    return fork.service.gitlab_instance.projects.get(f"{fork.namespace}/{fork.repo}")


def _wait_for_fork_ready(fork: GitlabProject) -> None:
    """Block until GitLab finishes provisioning a fork's git repository.

    Fork creation is asynchronous: the API returns before the repository
    accepts git pushes. Poll ``import_status`` until the fork is ready.
    """
    fork_path = f"{fork.namespace}/{fork.repo}"
    deadline = time.monotonic() + _FORK_READY_TIMEOUT_SEC
    while True:
        try:
            repo = _fork_api_project(fork)
            repo.refresh()
        except gitlab.GitlabError as exc:
            raise ToolError(f"Failed to query fork project {fork_path}") from exc

        status = repo.attributes.get("import_status", "none")
        if status in _FORK_READY_IMPORT_STATUSES:
            logger.info("Fork %s is ready (import_status=%s)", fork_path, status)
            return
        if status == "failed":
            import_error = repo.attributes.get("import_error") or "unknown error"
            raise ToolError(f"Fork {fork_path} failed to import: {import_error}")
        if time.monotonic() >= deadline:
            raise ToolError(
                f"Fork {fork_path} did not become ready within {_FORK_READY_TIMEOUT_SEC}s "
                f"(import_status={status})"
            )

        logger.info(
            "Fork %s not ready yet (import_status=%s), waiting %.0fs",
            fork_path,
            status,
            _FORK_READY_POLL_INTERVAL_SEC,
        )
        time.sleep(_FORK_READY_POLL_INTERVAL_SEC)


_GITLAB_COMMIT_RE = re.compile(r"^/(.+?)/-/commit/([0-9a-f]+)\.(?:patch|diff)$", re.IGNORECASE)
_REDHAT_WEB_PREFIX = "/redhat/"
_REDHAT_API_PREFIX = "/api/v4/projects/redhat%2F"


def _get_mock_git_env() -> dict[str, str] | None:
    """Build a subprocess ``env`` that scopes ``GIT_CONFIG_GLOBAL`` to the
    per-issue gitconfig when the MCP request carries a ``jira_issue`` in
    its ``_meta``.

    Falls back to ``None`` (inherit process env) when no per-issue
    gitconfig exists or when the request has no metadata.
    """
    try:
        ctx = request_ctx.get()
        meta = ctx.meta
    except LookupError:
        return None

    if meta is None:
        return None

    issue_key = getattr(meta, "jira_issue", None)
    if not issue_key:
        extra = getattr(meta, "__pydantic_extra__", None) or {}
        issue_key = extra.get("jira_issue")
    if not issue_key:
        return None

    base = Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos"))
    per_issue = base / f".mock_gitconfig_{issue_key}"
    if not per_issue.is_file():
        return None

    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = str(per_issue)
    logger.debug("Using per-issue gitconfig for %s: %s", issue_key, per_issue)
    return env


def _is_private_gitlab(url: str) -> bool:
    """Return True if *url* points to a Red Hat GitLab project that needs token auth."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname == "gitlab.cee.redhat.com":
        return True
    if hostname != "gitlab.com":
        return False
    if parsed.path.startswith(_REDHAT_WEB_PREFIX) or parsed.path.startswith(_REDHAT_API_PREFIX):
        return True
    fork_namespace = os.getenv("FORK_NAMESPACE", "").strip("/")
    return bool(fork_namespace and parsed.path.startswith(f"/{fork_namespace}/"))


def _get_api_diff_url(url: str) -> str:
    """Convert a GitLab commit .patch/.diff web URL to an API diff URL.

    Returns the API URL for private Red Hat GitLab repos, or the original URL
    unchanged for public repos and non-GitLab hosts.
    """
    if not _is_private_gitlab(url):
        return url

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    match = _GITLAB_COMMIT_RE.match(parsed.path)
    if not match:
        return url

    project_path = match.group(1)
    sha = match.group(2)
    encoded_path = quote(project_path, safe="")
    return f"{parsed.scheme}://{hostname}/api/v4/projects/{encoded_path}/repository/commits/{sha}/diff"


def _get_auth_headers(url: str) -> dict[str, str]:
    """Return headers for requests; always includes User-Agent, adds PRIVATE-TOKEN for private GitLab."""
    headers: dict[str, str] = {"User-Agent": YMIR_USER_AGENT}
    if _is_private_gitlab(url):
        token = os.getenv("GITLAB_TOKEN")
        if token:
            headers["PRIVATE-TOKEN"] = token
    return headers


_SENSITIVE_STDERR_RE = re.compile(
    r"authorization|basic\s+[A-Za-z0-9+/=]|token|password|credential",
    re.IGNORECASE,
)


def _sanitize_git_stderr(text: str) -> str:
    """Filter out lines from git stderr that may contain auth credentials."""
    return "\n".join(line for line in text.splitlines() if not _SENSITIVE_STDERR_RE.search(line))


def _remove_existing_clone_path(clone_path: Path) -> None:
    """Remove ``clone_path`` if it exists, only under allowed base directories."""
    if not clone_path.exists():
        return
    allowed_parents = {
        Path(os.environ.get("GIT_REPO_BASEPATH", "/git-repos")),
        Path("/tmp"),  # noqa: S108
    }
    if not any(clone_path.resolve().is_relative_to(p) for p in allowed_parents):
        raise ToolError(f"Refusing to remove {clone_path}: not under an allowed base directory")
    shutil.rmtree(clone_path)


def _get_git_auth_args(repository_url: str) -> list[str]:
    """Return ``git -c`` args that authenticate via HTTP Basic auth.

    Uses the same ``_is_private_gitlab`` guard as ``_get_auth_headers``
    so the token is never sent to unrelated hosts.  Passing auth via
    ``http.extraheader`` keeps the URL clean, which lets ``insteadOf``
    rewrites (mock repos) work without special-casing.

    Args:
        repository_url: The git remote URL to authenticate against.

    Returns:
        A list of ``-c http.extraheader=…`` args for private Red Hat
        GitLab repos, or an empty list otherwise.
    """
    if _is_private_gitlab(repository_url) and (token := os.getenv("GITLAB_TOKEN")):
        credentials = base64.b64encode(f"oauth2:{token}".encode()).decode()
        return ["-c", f"http.extraheader=Authorization: Basic {credentials}"]
    return []


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
    timeout = 120
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

        fork_namespace = os.getenv("FORK_NAMESPACE")

        def get_fork():
            target = fork_namespace or project.service.user.get_username()
            for fork in project.get_forks():
                if fork.gitlab_repo.namespace["full_path"] == target:
                    return fork
            return None

        if fork := await asyncio.to_thread(get_fork):
            return StringToolOutput(result=fork.get_git_urls()["git"])

        if os.getenv("DRY_RUN", "False").lower() == "true":
            logger.info("DRY_RUN is set, skipping fork creation — returning original repo URL")
            return StringToolOutput(result=project.get_git_urls()["git"])

        def create_fork():
            prefix = "_".join(ns.replace("centos-stream", "centos") for ns in namespace[1:])
            fork_name = (f"{prefix}_" if prefix else "") + project.gitlab_repo.name
            data = {"name": fork_name, "path": fork_name}
            if fork_namespace:
                data["namespace"] = fork_namespace
            try:
                fork = project.gitlab_repo.forks.create(data=data)
            except GitlabAPIException:
                if not fork_namespace:
                    raise
                logger.info("Fork creation failed, checking if it was created by another deployment")
                fork = get_fork()
                if not fork:
                    raise
                return fork
            return GitlabProject(
                namespace=fork.namespace["full_path"],
                service=project.service,
                repo=fork.path,
            )

        fork = await asyncio.to_thread(create_fork)
        if not fork:
            raise ToolError("Failed to fork the specified repository")
        await asyncio.to_thread(_wait_for_fork_ready, fork)
        return StringToolOutput(result=fork.get_git_urls()["git"])


class OpenMergeRequestToolInput(BaseModel):
    fork_url: str = Field(description="URL of the fork to open the MR from")
    title: str = Field(description="MR title")
    description: str = Field(description="MR description")
    target: str = Field(description="Target branch (in the original repository)")
    source: str = Field(description="Source branch (in the fork)")
    labels: list[str] | None = Field(
        default=None,
        description="Labels to set on the MR at creation time (atomic, avoids webhook race)",
    )


class OpenMergeRequestTool(
    Tool[
        OpenMergeRequestToolInput,
        ToolRunOptions,
        JSONToolOutput[OpenMergeRequestResult],
    ]
):
    name = "open_merge_request"
    timeout = 120
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

    @staticmethod
    def _create_mr(project, title, description, target, source, labels):
        parameters = {
            "source_branch": source,
            "target_branch": target,
            "title": title,
            "description": description,
        }
        if labels:
            parameters["labels"] = ",".join(labels)
        if project.is_fork:
            parameters["target_project_id"] = project.parent.gitlab_repo.attributes["id"]
        target_project = project.parent if project.is_fork else project
        try:
            raw_mr = project.gitlab_repo.mergerequests.create(parameters)
        except gitlab.GitlabError as ex:
            raise GitlabAPIException() from ex
        return GitlabPullRequest(raw_mr, target_project)

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
        labels = tool_input.labels
        logger.info(f"Connecting to GitLab API to open merge request from fork: {fork_url}")
        project = await asyncio.to_thread(get_project, url=fork_url, token=os.getenv("GITLAB_TOKEN"))
        if not project:
            raise ToolError("Failed to get the specified fork")
        is_new_mr = True
        try:
            pr = await asyncio.to_thread(self._create_mr, project, title, description, target, source, labels)
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
    timeout = 120
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
    branch: str | None = Field(default=None, description="Branch to clone. If omitted, all refs are fetched.")
    clone_path: AbsolutePath = Field(
        description="Absolute path under the shared /git-repos volume where to clone the repository"
    )


class CloneRepositoryTool(Tool[CloneRepositoryToolInput, ToolRunOptions, StringToolOutput]):
    name = "clone_repository"
    timeout = 3600
    description = """
    Clones the specified repository to the given local path.
    If branch is specified, only that branch is fetched and checked out.
    If branch is omitted, all refs are fetched (useful when you need access to
    specific commits across any branch).
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

        basepath = Path(os.getenv("GIT_REPO_BASEPATH", "/git-repos")).resolve()
        resolved = clone_path.resolve()
        if resolved == basepath or not resolved.is_relative_to(basepath):
            raise ToolError(f"clone_path must be under {basepath} (the shared volume). Got: {clone_path}")
        clone_path = resolved

        await clean_stale_repositories()

        auth_args = _get_git_auth_args(repository)
        git_env = _get_mock_git_env()

        safe_url = sanitize_url(repository)

        await asyncio.to_thread(_remove_existing_clone_path, clone_path)

        if branch:
            clone_path.mkdir(parents=True, exist_ok=True)
            await _run_git_cmd(
                ["git", "init"],
                label=f"git init {clone_path}",
                cwd=clone_path,
                env=git_env,
                timeout=None,
            )

            await _run_git_cmd(
                ["git", *auth_args, "fetch", repository, f"{branch}:refs/heads/{branch}"],
                label=f"git fetch {safe_url} branch={branch}",
                cwd=clone_path,
                env=git_env,
            )

            await _run_git_cmd(
                ["git", "checkout", branch],
                label=f"git checkout branch={branch}",
                cwd=clone_path,
                env=git_env,
                timeout=None,
            )
        else:
            clone_path.parent.mkdir(parents=True, exist_ok=True)
            await _run_git_cmd(
                ["git", *auth_args, "clone", repository, str(clone_path)],
                label=f"git clone {safe_url}",
                env=git_env,
            )

        return StringToolOutput(result=f"Successfully cloned the specified repository to {clone_path}")


class PushToRemoteRepositoryToolInput(BaseModel):
    repository: str = Field(description="Repository URL")
    clone_path: AbsolutePath = Field(description="Absolute path to local clone of the repository")
    branch: str = Field(description="Branch to push")
    force: bool = Field(default=False, description="Whether to overwrite the remote ref")


class PushToRemoteRepositoryTool(Tool[PushToRemoteRepositoryToolInput, ToolRunOptions, StringToolOutput]):
    name = "push_to_remote_repository"
    timeout = 3600
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
        branch = tool_input.branch
        clone_path = tool_input.clone_path
        force = tool_input.force
        safe_url = sanitize_url(repository)
        auth_args = _get_git_auth_args(repository)
        git_env = _get_mock_git_env()

        command = ["git", *auth_args, "push", repository, branch]
        if force:
            command.append("--force")

        await _run_git_cmd(
            command,
            label=f"git push {safe_url} branch={branch} force={force}",
            cwd=clone_path,
            env=git_env,
            timeout=None,
        )

        return StringToolOutput(result=f"Successfully pushed the specified branch to {safe_url}")


class FetchBranchToolInput(BaseModel):
    repository: str = Field(description="Remote repository URL to fetch from")
    branch: str = Field(description="Branch name to fetch")
    clone_path: AbsolutePath = Field(description="Absolute path to the local clone")


class FetchBranchTool(Tool[FetchBranchToolInput, ToolRunOptions, StringToolOutput]):
    name = "fetch_branch"
    timeout = 3600
    description = """
    Fetches a single branch from a remote repository into a local clone.
    The branch is created as a local ref.
    """
    input_schema = FetchBranchToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: FetchBranchToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        repository = tool_input.repository
        branch = tool_input.branch
        clone_path = tool_input.clone_path
        safe_url = sanitize_url(repository)
        auth_args = _get_git_auth_args(repository)
        git_env = _get_mock_git_env()

        await _run_git_cmd(
            ["git", *auth_args, "fetch", repository, f"{branch}:refs/heads/{branch}"],
            label=f"git fetch {safe_url} branch={branch}",
            cwd=clone_path,
            env=git_env,
            timeout=None,
        )

        return StringToolOutput(result=f"Successfully fetched branch {branch} from {safe_url}")


class AddMergeRequestLabelsToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")
    labels: list[str] = Field(description="List of labels to add to the merge request")


class AddMergeRequestLabelsTool(Tool[AddMergeRequestLabelsToolInput, ToolRunOptions, StringToolOutput]):
    name = "add_merge_request_labels"
    timeout = 120
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
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to add labels to merge request: {e}") from e


class SetMergeRequestReviewersToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")
    reviewer_ids: list[int] = Field(description="List of GitLab user IDs to set as reviewers")


class SetMergeRequestReviewersTool(Tool[SetMergeRequestReviewersToolInput, ToolRunOptions, StringToolOutput]):
    name = "set_merge_request_reviewers"
    timeout = 120
    description = """
    Sets reviewers on an existing merge request.
    """
    input_schema = SetMergeRequestReviewersToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: SetMergeRequestReviewersToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        merge_request_url = tool_input.merge_request_url
        reviewer_ids = tool_input.reviewer_ids
        try:
            mr = await _get_merge_request_from_url(merge_request_url)

            def set_reviewers():
                mr._raw_pr.reviewer_ids = reviewer_ids
                mr._raw_pr.save()

            await asyncio.to_thread(set_reviewers)
            return StringToolOutput(
                result=f"Successfully set reviewers {reviewer_ids} on merge request {merge_request_url}"
            )
        except Exception as e:
            raise ToolError(f"Failed to set reviewers on merge request: {e}") from e


class ResolveReviewersToolInput(BaseModel):
    package: str = Field(description="RPM package name")
    dist_git_branch: str = Field(description="Target dist-git branch")


class ResolveReviewersTool(Tool[ResolveReviewersToolInput, ToolRunOptions, JSONToolOutput[list[int]]]):
    name = "resolve_reviewers"
    timeout = 120
    description = """
    Resolve reviewer GitLab user IDs for a package from bugzilla component contacts.
    """
    input_schema = ResolveReviewersToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: ResolveReviewersToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[int]]:
        from ymir.tools.privileged.reviewer_resolver import resolve_reviewers

        reviewer_ids = await resolve_reviewers(tool_input.package, tool_input.dist_git_branch)
        return JSONToolOutput(result=reviewer_ids)


class ResolveQeReviewersTool(Tool[ResolveReviewersToolInput, ToolRunOptions, JSONToolOutput[list[int]]]):
    name = "resolve_qe_reviewers"
    timeout = 120
    description = """
    Resolve QE reviewer GitLab user IDs for a package from the bugzilla QA Contact.
    """
    input_schema = ResolveReviewersToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: ResolveReviewersToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[int]]:
        from ymir.tools.privileged.reviewer_resolver import resolve_qe_reviewers

        reviewer_ids = await resolve_qe_reviewers(tool_input.package, tool_input.dist_git_branch)
        return JSONToolOutput(result=reviewer_ids)


class AddMergeRequestCommentToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")
    comment: str = Field(description="Comment text")


class AddMergeRequestCommentTool(Tool[AddMergeRequestCommentToolInput, ToolRunOptions, StringToolOutput]):
    name = "add_merge_request_comment"
    timeout = 120
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
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to add comment to merge request: {e}") from e


class AddBlockingMergeRequestCommentToolInput(BaseModel):
    merge_request_url: str = Field(description="URL of the merge request")
    comment: str = Field(description="Comment text to add as a blocking discussion")


class AddBlockingMergeRequestCommentTool(
    Tool[AddBlockingMergeRequestCommentToolInput, ToolRunOptions, StringToolOutput]
):
    name = "add_blocking_merge_request_comment"
    timeout = 120
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
    timeout = 120
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
    timeout = 120
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
                        allow_failure=getattr(job, "allow_failure", False),
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
    timeout = 120
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
        except ToolError:
            raise
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
    timeout = 120
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
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to get merge request details: {e}") from e


MAX_PATCH_CONTENT_LENGTH = 2000


class GetPatchFromUrlToolInput(BaseModel):
    patch_url: str = Field(description="URL to a patch or diff file")


class GetPatchFromUrlTool(Tool[GetPatchFromUrlToolInput, ToolRunOptions, StringToolOutput]):
    name = "get_patch_from_url"
    timeout = 120
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

    @staticmethod
    def _json_hunks_to_text(hunks: list[dict]) -> str:
        parts = []
        for hunk in hunks:
            old_path = hunk.get("old_path", "")
            new_path = hunk.get("new_path", "")
            parts.append(f"--- a/{old_path}")
            parts.append(f"+++ b/{new_path}")
            parts.append(hunk.get("diff", ""))
        return "\n".join(parts)

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
                aiohttp_get_with_retries(session, request_url, headers=headers) as response,
            ):
                if response.status >= 400:
                    # We return Error string instead of ToolError, because status >= 400
                    # on for example badly formatted URL is not a tool error and
                    # should not be flagged
                    return StringToolOutput(
                        result=f"Error: Failed to fetch patch from {patch_url}: HTTP {response.status}"
                    )
                text = await response.text()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ToolError(f"Failed to fetch patch from {patch_url}: {e}") from e
        try:
            hunks = json.loads(text)
        except json.decoder.JSONDecodeError:
            pass
        else:
            if isinstance(hunks, list):
                text = self._json_hunks_to_text(hunks)
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
    timeout = 120
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
                aiohttp_get_with_retries(
                    session,
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

        except (aiohttp.ClientError, TimeoutError) as e:
            # Here we handle ClientError as ToolError, because client error
            # signals networking issues which should be flagged (DNS resolution failure, timeouts etc)
            raise ToolError(f"Failed to fetch MR notes for !{input.mr_iid} in {input.project}: {e}") from e
        except Exception as e:
            logger.error("Error fetching GitLab MR notes: %s", e)
            return StringToolOutput(result=f"Error fetching GitLab MR notes: {e}")


class SearchGitlabProjectMrsToolInput(BaseModel):
    project: str = Field(description="GitLab project path (e.g. 'redhat/rhel/rpms/podman')")
    search: str = Field(description="Search string to match against merge requests (e.g. a JIRA issue key)")
    state: str | None = Field(
        default=None,
        description="Filter MRs by state: 'opened', 'closed', 'merged', or null for all",
    )


class SearchGitlabProjectMrsTool(
    Tool[
        SearchGitlabProjectMrsToolInput,
        ToolRunOptions,
        JSONToolOutput[list[dict[str, Any]]],
    ]
):
    name = "search_gitlab_project_mrs"
    timeout = 120
    description = """
    Searches for merge requests in a GitLab project matching a search string
    (typically a JIRA issue key). Returns a list of matching MRs with their
    project, iid, url, title, description, state, and merged_at timestamp.
    """
    input_schema = SearchGitlabProjectMrsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: SearchGitlabProjectMrsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        project = tool_input.project
        search = tool_input.search
        state = tool_input.state

        encoded_project = quote(project, safe="")
        url = f"https://gitlab.com/api/v4/projects/{encoded_project}/merge_requests"

        params: dict[str, str] = {"search": search}
        if state is not None:
            params["state"] = state

        headers = _get_auth_headers(f"https://gitlab.com/{project}")
        logger.info("Searching MRs for %s in %s (state=%s)", search, project, state)

        try:
            async with (
                aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                aiohttp_get_with_retries(session, url, headers=headers, params=params) as response,
            ):
                response.raise_for_status()
                data = await response.json()

            results = [
                {
                    "project": project,
                    "iid": mr["iid"],
                    "url": mr["web_url"],
                    "title": mr["title"],
                    "description": mr.get("description", ""),
                    "state": mr["state"],
                    "merged_at": mr.get("merged_at"),
                }
                for mr in data
            ]

            logger.info("Found %d MR(s) for %s in %s", len(results), search, project)
            return JSONToolOutput(result=results)

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to search MRs in {project}: {e}") from e


class ListProjectMergeRequestsToolInput(BaseModel):
    project: str = Field(description="Full GitLab project path (e.g. 'redhat/centos-stream/rpms/bash')")
    state: str | None = Field(
        default="opened",
        description="Filter by state: 'opened', 'closed', 'merged', or None for all",
    )
    target_branch: str | None = Field(
        default=None,
        description="Filter by target branch (e.g. 'c10s')",
    )
    labels: list[str] | None = Field(
        default=None,
        description="Filter by labels (all must be present)",
    )
    author_username: str | None = Field(
        default=None,
        description="Filter by MR author username",
    )
    order_by: str = Field(
        default="created_at",
        description="Order by field: 'created_at' or 'updated_at'",
    )
    sort: str = Field(default="asc", description="Sort direction: 'asc' or 'desc'")


class ListProjectMergeRequestsTool(
    Tool[
        ListProjectMergeRequestsToolInput,
        ToolRunOptions,
        JSONToolOutput[list[dict[str, Any]]],
    ]
):
    name = "list_project_merge_requests"
    timeout = 120
    description = """
    Lists merge requests for a GitLab project with filtering by state,
    target branch, labels, and author. Returns MRs sorted by creation date
    (ascending by default, i.e. oldest first).
    Returns at most 100 MRs (single page, no pagination).
    """
    input_schema = ListProjectMergeRequestsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "gitlab", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: ListProjectMergeRequestsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        encoded_project = quote(tool_input.project, safe="")
        url = f"https://gitlab.com/api/v4/projects/{encoded_project}/merge_requests"

        params: dict[str, str] = {
            "order_by": tool_input.order_by,
            "sort": tool_input.sort,
            "per_page": "100",
        }
        if tool_input.state is not None:
            params["state"] = tool_input.state
        if tool_input.target_branch is not None:
            params["target_branch"] = tool_input.target_branch
        if tool_input.labels:
            params["labels"] = ",".join(tool_input.labels)
        if tool_input.author_username is not None:
            params["author_username"] = tool_input.author_username

        headers = _get_auth_headers(f"https://gitlab.com/{tool_input.project}")
        logger.info(
            "Listing MRs for project %s (state=%s, target=%s, labels=%s, author=%s)",
            tool_input.project,
            tool_input.state,
            tool_input.target_branch,
            tool_input.labels,
            tool_input.author_username,
        )

        try:
            async with (
                aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                aiohttp_get_with_retries(session, url, headers=headers, params=params) as response,
            ):
                response.raise_for_status()
                data = await response.json()

            results = [
                {
                    "iid": mr["iid"],
                    "url": mr["web_url"],
                    "title": mr["title"],
                    "description": mr.get("description", ""),
                    "state": mr["state"],
                    "source_branch": mr.get("source_branch", ""),
                    "target_branch": mr.get("target_branch", ""),
                    "author": mr.get("author", {}).get("username", ""),
                    "created_at": mr.get("created_at"),
                    "labels": mr.get("labels", []),
                }
                for mr in data
            ]

            logger.info("Found %d MR(s) for project %s", len(results), tool_input.project)
            return JSONToolOutput(result=results)

        except Exception as e:
            raise ToolError(f"Failed to list MRs for {tool_input.project}: {e}") from e


# ---------------------------------------------------------------------------
# Review / QE tools — pipeline triage and ship-mr support
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?(?:\x07|\x1b\\)")


def _strip_ansi(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


async def _gitlab_api_get(path: str, params: dict | None = None) -> Any:
    """GET from the GitLab API, authenticating with GITLAB_TOKEN."""
    token = os.getenv("GITLAB_TOKEN", "")
    base = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    url = f"{base}/api/v4{path}"
    headers: dict[str, str] = {"User-Agent": YMIR_USER_AGENT}
    if token:
        headers["PRIVATE-TOKEN"] = token
    async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
        async with aiohttp_get_with_retries(session, url, headers=headers, params=params or {}) as resp:
            resp.raise_for_status()
            return await resp.json()


async def _gitlab_api_post(path: str, body: dict) -> Any:
    token = os.getenv("GITLAB_TOKEN", "")
    base = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    url = f"{base}/api/v4{path}"
    headers = {"User-Agent": YMIR_USER_AGENT, "Content-Type": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
        async with session.post(url, json=body, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()


async def _gitlab_api_put(path: str, body: dict) -> Any:
    token = os.getenv("GITLAB_TOKEN", "")
    base = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    url = f"{base}/api/v4{path}"
    headers = {"User-Agent": YMIR_USER_AGENT, "Content-Type": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
        async with session.put(url, json=body, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()


async def _get_job_trace(project_enc: str, job_id: int) -> str:
    token = os.getenv("GITLAB_TOKEN", "")
    base = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    url = f"{base}/api/v4/projects/{project_enc}/jobs/{job_id}/trace"
    headers: dict[str, str] = {"User-Agent": YMIR_USER_AGENT}
    if token:
        headers["PRIVATE-TOKEN"] = token
    try:
        async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status >= 400:
                    return ""
                return _strip_ansi(await resp.text())
    except Exception:
        return ""


async def _collect_failed_jobs_recursive(
    project_enc: str, pipeline_id: int, log_lines: int, depth: int = 0
) -> list[dict]:
    if depth > 3:
        return []
    output: list[dict] = []
    try:
        jobs = await _gitlab_api_get(
            f"/projects/{project_enc}/pipelines/{pipeline_id}/jobs",
            params={"scope[]": "failed", "per_page": "100"},
        )
    except Exception:
        jobs = []
    for job in jobs:
        trace = await _get_job_trace(project_enc, job["id"])
        trace_lines = trace.splitlines()
        tail = "\n".join(trace_lines[-log_lines:]) if len(trace_lines) > log_lines else trace
        output.append(
            {
                "job_id": job["id"],
                "name": job.get("name", ""),
                "stage": job.get("stage", ""),
                "status": job.get("status", ""),
                "failure_reason": job.get("failure_reason", ""),
                "web_url": job.get("web_url", ""),
                "log_tail": tail,
                "pipeline_id": pipeline_id,
            }
        )
    # Recurse into child pipelines via bridges
    try:
        bridges = await _gitlab_api_get(
            f"/projects/{project_enc}/pipelines/{pipeline_id}/bridges",
            params={"per_page": "50"},
        )
    except Exception:
        bridges = []
    for bridge in bridges:
        downstream = bridge.get("downstream_pipeline")
        if not downstream:
            continue
        child_proj = str(downstream.get("project_id", "")) or project_enc
        child_output = await _collect_failed_jobs_recursive(
            child_proj, downstream["id"], log_lines, depth + 1
        )
        output.extend(child_output)
    return output


# -- GetMrPipelinesWithStatusTool --


class GetMrPipelinesWithStatusToolInput(BaseModel):
    project: str = Field(description="GitLab project path (e.g. 'redhat/centos-stream/rpms/bash')")
    mr_iid: int = Field(description="Merge request IID (the number after !)")


class GetMrPipelinesWithStatusTool(
    Tool[GetMrPipelinesWithStatusToolInput, ToolRunOptions, JSONToolOutput[list[dict[str, Any]]]]
):
    name = "get_mr_pipelines_with_status"
    timeout = 120
    description = """
    Lists all pipelines for a merge request, including child/downstream pipelines
    triggered via bridge jobs. Returns status, ref, SHA, web_url, and child pipeline info.
    Use this to detect whether the latest pipeline passed, failed, or is still running.
    """
    input_schema = GetMrPipelinesWithStatusToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetMrPipelinesWithStatusToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        project_enc = quote(tool_input.project, safe="")
        try:
            pipelines = await _gitlab_api_get(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}/pipelines",
                params={"per_page": "20"},
            )
        except Exception as e:
            raise ToolError(f"Failed to get pipelines for !{tool_input.mr_iid}: {e}") from e

        results = []
        for p in pipelines:
            entry: dict[str, Any] = {
                "id": p["id"],
                "status": p.get("status", ""),
                "ref": p.get("ref", ""),
                "sha": p.get("sha", "")[:8],
                "web_url": p.get("web_url", ""),
                "created_at": p.get("created_at", ""),
                "child_pipelines": [],
            }
            try:
                bridges = await _gitlab_api_get(
                    f"/projects/{project_enc}/pipelines/{p['id']}/bridges",
                    params={"per_page": "50"},
                )
                for bridge in bridges:
                    downstream = bridge.get("downstream_pipeline")
                    if downstream:
                        entry["child_pipelines"].append(
                            {
                                "id": downstream["id"],
                                "status": downstream.get("status", ""),
                                "web_url": downstream.get("web_url", ""),
                                "triggered_by": bridge.get("name", ""),
                            }
                        )
            except Exception:
                pass
            results.append(entry)
        return JSONToolOutput(result=results)


# -- GetMrChangesTool --


class GetMrChangesToolInput(BaseModel):
    project: str = Field(description="GitLab project path (e.g. 'redhat/centos-stream/rpms/bash')")
    mr_iid: int = Field(description="Merge request IID")


class GetMrChangesTool(Tool[GetMrChangesToolInput, ToolRunOptions, JSONToolOutput[list[dict[str, Any]]]]):
    name = "get_mr_changes"
    timeout = 120
    description = """
    Returns the full diff of all changed files in a merge request.
    Each entry includes old_path, new_path, and the unified diff text.
    Also returns diff_refs (base_sha, start_sha, head_sha) needed for inline comments.
    """
    input_schema = GetMrChangesToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetMrChangesToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        project_enc = quote(tool_input.project, safe="")
        try:
            data = await _gitlab_api_get(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}/changes"
            )
        except Exception as e:
            raise ToolError(f"Failed to get MR changes for !{tool_input.mr_iid}: {e}") from e

        diff_refs = data.get("diff_refs", {})
        changes = [
            {
                "old_path": c.get("old_path", ""),
                "new_path": c.get("new_path", ""),
                "new_file": c.get("new_file", False),
                "deleted_file": c.get("deleted_file", False),
                "renamed_file": c.get("renamed_file", False),
                "diff": c.get("diff", ""),
                "diff_refs": diff_refs,
            }
            for c in data.get("changes", [])
        ]
        return JSONToolOutput(result=changes)


# -- GetMrDiscussionsTool --


class GetMrDiscussionsToolInput(BaseModel):
    project: str = Field(description="GitLab project path")
    mr_iid: int = Field(description="Merge request IID")


class GetMrDiscussionsTool(
    Tool[GetMrDiscussionsToolInput, ToolRunOptions, JSONToolOutput[list[dict[str, Any]]]]
):
    name = "get_mr_discussions"
    timeout = 120
    description = """
    Returns all discussion threads and comments on a merge request.
    Each thread includes its discussion_id, resolved state, position (for inline comments),
    and all notes (author, body, created_at).
    Use this to find pipeline result threads, existing review comments, or waiver messages.
    """
    input_schema = GetMrDiscussionsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetMrDiscussionsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        project_enc = quote(tool_input.project, safe="")
        try:
            discussions = await _gitlab_api_get(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}/discussions",
                params={"per_page": "100"},
            )
        except Exception as e:
            raise ToolError(f"Failed to get discussions for !{tool_input.mr_iid}: {e}") from e

        results = []
        for disc in discussions:
            notes = disc.get("notes", [])
            if not notes:
                continue
            first = notes[0]
            position = first.get("position")
            results.append(
                {
                    "discussion_id": disc.get("id", ""),
                    "resolved": first.get("resolved", False),
                    "position": {
                        "new_path": position.get("new_path", "") if position else "",
                        "new_line": position.get("new_line") if position else None,
                    }
                    if position
                    else None,
                    "notes": [
                        {
                            "author": n.get("author", {}).get("username", ""),
                            "body": n.get("body", ""),
                            "created_at": n.get("created_at", ""),
                            "system": n.get("system", False),
                        }
                        for n in notes
                    ],
                }
            )
        return JSONToolOutput(result=results)


# -- ReplyToMrDiscussionTool --


class ReplyToMrDiscussionToolInput(BaseModel):
    project: str = Field(description="GitLab project path")
    mr_iid: int = Field(description="Merge request IID")
    discussion_id: str = Field(description="ID of the discussion thread to reply to")
    body: str = Field(description="Reply text (supports GitLab Markdown)")


class ReplyToMrDiscussionTool(Tool[ReplyToMrDiscussionToolInput, ToolRunOptions, StringToolOutput]):
    name = "reply_to_mr_discussion"
    timeout = 120
    description = """
    Replies to an existing discussion thread on a merge request.
    Use get_mr_discussions first to find the discussion_id.
    Use this to post pipeline triage results in-context on the pipeline failure thread.
    """
    input_schema = ReplyToMrDiscussionToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: ReplyToMrDiscussionToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        project_enc = quote(tool_input.project, safe="")
        try:
            result = await _gitlab_api_post(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}"
                f"/discussions/{tool_input.discussion_id}/notes",
                body={"body": tool_input.body},
            )
        except Exception as e:
            raise ToolError(f"Failed to reply to discussion {tool_input.discussion_id}: {e}") from e
        return StringToolOutput(result=f"Reply posted (note ID: {result.get('id', '?')})")


# -- ResolveMrDiscussionTool --


class ResolveMrDiscussionToolInput(BaseModel):
    project: str = Field(description="GitLab project path")
    mr_iid: int = Field(description="Merge request IID")
    discussion_id: str = Field(description="ID of the discussion thread to resolve")
    resolved: bool = Field(default=True, description="True to resolve, False to unresolve")


class ResolveMrDiscussionTool(Tool[ResolveMrDiscussionToolInput, ToolRunOptions, StringToolOutput]):
    name = "resolve_mr_discussion"
    timeout = 120
    description = """
    Resolves or unresolves a discussion thread on a merge request.
    Use get_mr_discussions to find discussion_id values.
    Resolving all open discussions is required before approving a merge request.
    """
    input_schema = ResolveMrDiscussionToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: ResolveMrDiscussionToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        project_enc = quote(tool_input.project, safe="")
        try:
            await _gitlab_api_put(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}"
                f"/discussions/{tool_input.discussion_id}",
                body={"resolved": tool_input.resolved},
            )
        except Exception as e:
            raise ToolError(f"Failed to resolve discussion {tool_input.discussion_id}: {e}") from e
        action = "Resolved" if tool_input.resolved else "Unresolved"
        return StringToolOutput(result=f"{action} discussion {tool_input.discussion_id}")


# -- ApproveMergeRequestTool --


class ApproveMergeRequestToolInput(BaseModel):
    project: str = Field(description="GitLab project path")
    mr_iid: int = Field(description="Merge request IID")


class ApproveMergeRequestTool(Tool[ApproveMergeRequestToolInput, ToolRunOptions, StringToolOutput]):
    name = "approve_merge_request"
    timeout = 120
    description = """
    Approves a merge request. Requires your GITLAB_TOKEN to have appropriate permissions.
    Call this only after all review checks pass and all discussions are resolved.
    """
    input_schema = ApproveMergeRequestToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: ApproveMergeRequestToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        project_enc = quote(tool_input.project, safe="")
        try:
            await _gitlab_api_post(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}/approve",
                body={},
            )
        except Exception as e:
            raise ToolError(f"Failed to approve MR !{tool_input.mr_iid}: {e}") from e
        return StringToolOutput(result=f"MR !{tool_input.mr_iid} approved successfully.")


# -- SetMrAutoMergeTool --


class SetMrAutoMergeToolInput(BaseModel):
    project: str = Field(description="GitLab project path")
    mr_iid: int = Field(description="Merge request IID")


class SetMrAutoMergeTool(Tool[SetMrAutoMergeToolInput, ToolRunOptions, StringToolOutput]):
    name = "set_mr_auto_merge"
    timeout = 120
    description = """
    Enables auto-merge (merge when pipeline succeeds) on a merge request.
    The MR must have an active pipeline and all required approvals.
    Call this after approving the MR when the pipeline is still running.
    """
    input_schema = SetMrAutoMergeToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: SetMrAutoMergeToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        project_enc = quote(tool_input.project, safe="")
        try:
            await _gitlab_api_put(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}/merge",
                body={"merge_when_pipeline_succeeds": True},
            )
        except Exception as e:
            raise ToolError(f"Failed to set auto-merge on !{tool_input.mr_iid}: {e}") from e
        return StringToolOutput(
            result=f"Auto-merge enabled on !{tool_input.mr_iid}. It will merge when the pipeline succeeds."
        )


# -- GetPipelineFailedJobsDeepTool --


class GetPipelineFailedJobsDeepToolInput(BaseModel):
    project: str = Field(description="GitLab project path")
    pipeline_id: int = Field(description="Pipeline ID (numeric)")
    log_lines: int = Field(default=100, description="Number of log tail lines to include per failed job")


class GetPipelineFailedJobsDeepTool(
    Tool[GetPipelineFailedJobsDeepToolInput, ToolRunOptions, JSONToolOutput[list[dict[str, Any]]]]
):
    name = "get_pipeline_failed_jobs_deep"
    timeout = 180
    description = """
    Gets failed job details and log output for a pipeline, including child/downstream
    pipelines triggered via bridge jobs. Returns job name, stage, failure reason,
    web_url, and the last N lines of the job log for each failed job.
    Use get_mr_pipelines_with_status first to find the pipeline ID.
    """
    input_schema = GetPipelineFailedJobsDeepToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetPipelineFailedJobsDeepToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[list[dict[str, Any]]]:
        project_enc = quote(tool_input.project, safe="")
        try:
            failed = await _collect_failed_jobs_recursive(
                project_enc, tool_input.pipeline_id, tool_input.log_lines
            )
        except Exception as e:
            raise ToolError(f"Failed to get failed jobs for pipeline #{tool_input.pipeline_id}: {e}") from e
        return JSONToolOutput(result=failed)


# -- CompareMrTestFailuresTool --


class CompareMrTestFailuresToolInput(BaseModel):
    project: str = Field(description="GitLab project path")
    mr_iid: int = Field(description="Merge request IID")


class CompareMrTestFailuresTool(
    Tool[CompareMrTestFailuresToolInput, ToolRunOptions, JSONToolOutput[dict[str, Any]]]
):
    name = "compare_mr_test_failures"
    timeout = 180
    description = """
    Compares pipeline test failures for this MR against the previous merged MR on the
    same target branch. Returns:
      - new_failures: job names that failed in this MR but NOT the previous one (regressions)
      - waived_failures: job names that failed in BOTH this and the previous MR (pre-existing)
      - fixed: job names that failed previously but pass now
    Use this to determine whether failures are new regressions or pre-existing issues.
    """
    input_schema = CompareMrTestFailuresToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: CompareMrTestFailuresToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[dict[str, Any]]:
        project_enc = quote(tool_input.project, safe="")
        try:
            mr = await _gitlab_api_get(f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}")
            target_branch = mr.get("target_branch", "")

            # Get current MR pipelines and collect failed job names
            current_pipelines = await _gitlab_api_get(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}/pipelines",
                params={"per_page": "5"},
            )
            current_failed: set[str] = set()
            for p in current_pipelines[:1]:  # only latest pipeline
                jobs = await _collect_failed_jobs_recursive(project_enc, p["id"], log_lines=10)
                current_failed.update(j["name"] for j in jobs)

            # Find previous merged MR on same target branch
            prev_mrs = await _gitlab_api_get(
                f"/projects/{project_enc}/merge_requests",
                params={
                    "state": "merged",
                    "target_branch": target_branch,
                    "order_by": "merged_at",
                    "sort": "desc",
                    "per_page": "5",
                },
            )
            prev_mr = next((m for m in prev_mrs if m["iid"] != tool_input.mr_iid), None)

            prev_failed: set[str] = set()
            prev_iid = None
            if prev_mr:
                prev_iid = prev_mr["iid"]
                prev_pipelines = await _gitlab_api_get(
                    f"/projects/{project_enc}/merge_requests/{prev_iid}/pipelines",
                    params={"per_page": "5"},
                )
                for p in prev_pipelines[:1]:
                    jobs = await _collect_failed_jobs_recursive(project_enc, p["id"], log_lines=10)
                    prev_failed.update(j["name"] for j in jobs)

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to compare test failures for !{tool_input.mr_iid}: {e}") from e

        new_failures = sorted(current_failed - prev_failed)
        waived_failures = sorted(current_failed & prev_failed)
        fixed = sorted(prev_failed - current_failed)

        return JSONToolOutput(
            result={
                "mr_iid": tool_input.mr_iid,
                "previous_mr_iid": prev_iid,
                "target_branch": target_branch,
                "new_failures": new_failures,
                "waived_failures": waived_failures,
                "fixed": fixed,
                "total_current_failures": len(current_failed),
            }
        )


# -- CreateMrInlineCommentTool --


class CreateMrInlineCommentToolInput(BaseModel):
    project: str = Field(description="GitLab project path")
    mr_iid: int = Field(description="Merge request IID")
    body: str = Field(description="Comment text (supports GitLab Markdown)")
    new_path: str = Field(description="File path in the new version of the diff")
    new_line: int = Field(description="Line number in the new version of the file")
    base_sha: str = Field(default="", description="base_sha from diff_refs (get_mr_changes returns this)")
    start_sha: str = Field(default="", description="start_sha from diff_refs")
    head_sha: str = Field(default="", description="head_sha from diff_refs")


class CreateMrInlineCommentTool(Tool[CreateMrInlineCommentToolInput, ToolRunOptions, StringToolOutput]):
    name = "create_mr_inline_comment"
    timeout = 120
    description = """
    Posts an inline review comment at a specific file and line in the MR diff.
    Use get_mr_changes first to get the diff_refs (base_sha, start_sha, head_sha)
    and file paths. Use this for code review findings on specific lines.
    """
    input_schema = CreateMrInlineCommentToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "gitlab", self.name], creator=self)

    async def _run(
        self,
        tool_input: CreateMrInlineCommentToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        project_enc = quote(tool_input.project, safe="")
        base_sha = tool_input.base_sha
        start_sha = tool_input.start_sha
        head_sha = tool_input.head_sha

        # Auto-fetch diff_refs if not provided
        if not (base_sha and start_sha and head_sha):
            try:
                mr = await _gitlab_api_get(f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}")
                diff_refs = mr.get("diff_refs", {})
                base_sha = base_sha or diff_refs.get("base_sha", "")
                start_sha = start_sha or diff_refs.get("start_sha", "")
                head_sha = head_sha or diff_refs.get("head_sha", "")
            except Exception as e:
                raise ToolError(f"Failed to fetch diff_refs: {e}") from e

        position = {
            "position_type": "text",
            "base_sha": base_sha,
            "start_sha": start_sha,
            "head_sha": head_sha,
            "new_path": tool_input.new_path,
            "old_path": tool_input.new_path,
            "new_line": tool_input.new_line,
        }
        try:
            result = await _gitlab_api_post(
                f"/projects/{project_enc}/merge_requests/{tool_input.mr_iid}/discussions",
                body={"body": tool_input.body, "position": position},
            )
        except Exception as e:
            raise ToolError(f"Failed to create inline comment: {e}") from e
        return StringToolOutput(result=f"Inline comment created (discussion ID: {result.get('id', '?')})")
