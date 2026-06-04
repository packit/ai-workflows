import asyncio
import hashlib
import logging
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

from beeai_framework.tools import Tool
from specfile import Specfile

from ymir.agents.constants import BRANCH_PREFIX, JIRA_COMMENT_TEMPLATE
from ymir.agents.utils import check_subprocess, mcp_tools, run_subprocess, run_tool
from ymir.common.base_utils import is_cs_branch
from ymir.common.models import (
    CachedMRMetadata,
    LogOutputSchema,
    MergeRequestDetails,
    OpenMergeRequestResult,
    Task,
)
from ymir.tools.unprivileged.specfile import UpdateReleaseTool

logger = logging.getLogger(__name__)


async def _clone_fedora_dist_git(package: str, destination: Path) -> bool:
    try:
        if destination.is_dir():
            shutil.rmtree(destination, ignore_errors=False)
        await check_subprocess(
            [
                "git",
                "clone",
                "--single-branch",
                "--branch",
                "rawhide",
                f"https://src.fedoraproject.org/rpms/{package}",
                str(destination),
            ],
        )
    except Exception as e:
        logger.warning(f"Failed to clone Fedora repository for {package}: {e}")
        return False
    return True


async def fork_and_prepare_dist_git(
    jira_issue: str,
    package: str,
    dist_git_branch: str,
    available_tools: list[Tool],
    with_fedora: bool = False,
) -> tuple[Path, str, str, Path | None]:
    if not jira_issue or Path(jira_issue).is_absolute() or ".." in jira_issue:
        raise ValueError(f"Invalid jira_issue: {jira_issue}")
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / jira_issue
    if working_dir.is_dir():
        shutil.rmtree(working_dir, ignore_errors=False)
    working_dir.mkdir(parents=True, exist_ok=True)
    namespace = "centos-stream" if is_cs_branch(dist_git_branch) else "rhel"
    repository = f"https://gitlab.com/redhat/{namespace}/rpms/{package}"
    fork_url = await run_tool("fork_repository", repository=repository, available_tools=available_tools)
    local_clone = working_dir / package
    if not is_cs_branch(dist_git_branch):
        await run_tool(
            "create_zstream_branch",
            package=package,
            branch=dist_git_branch,
            available_tools=available_tools,
        )
    await run_tool(
        "clone_repository",
        repository=repository,
        branch=dist_git_branch,
        clone_path=str(local_clone),
        available_tools=available_tools,
    )
    update_branch = f"{BRANCH_PREFIX}-{jira_issue}"
    await check_subprocess(["git", "checkout", "-B", update_branch], cwd=local_clone)
    fedora_clone = None
    if with_fedora:
        fedora_clone = working_dir / f"{package}-fedora"
        if not await _clone_fedora_dist_git(package, fedora_clone):
            fedora_clone = None
    return local_clone, update_branch, fork_url, fedora_clone


async def prepare_dist_git_from_merge_request(
    merge_request_url: str,
    available_tools: list[Tool],
    with_fedora: bool = False,
) -> tuple[Path, MergeRequestDetails, Path | None]:
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / "merge_requests"
    working_dir.mkdir(parents=True, exist_ok=True)
    local_clone = working_dir / urlparse(merge_request_url).path.replace("/", "_")
    shutil.rmtree(local_clone, ignore_errors=True)
    details = await run_tool(
        "get_merge_request_details",
        merge_request_url=merge_request_url,
        available_tools=available_tools,
    )
    details = MergeRequestDetails.model_validate(details)
    await run_tool(
        "clone_repository",
        repository=details.source_repo,
        branch=details.source_branch,
        clone_path=str(local_clone),
        available_tools=available_tools,
    )
    fedora_clone = None
    if with_fedora:
        package = details.target_repo_name
        fedora_clone = working_dir / f"{package}-fedora"
        if not await _clone_fedora_dist_git(package, fedora_clone):
            fedora_clone = None
    return local_clone, details, fedora_clone


async def update_release(
    local_clone: Path,
    package: str,
    dist_git_branch: str,
    rebase: bool,
    abandon_autorelease: bool = False,
) -> None:
    await run_tool(
        UpdateReleaseTool(options={"working_directory": local_clone}),
        spec=f"{package}.spec",
        package=package,
        dist_git_branch=dist_git_branch,
        rebase=rebase,
        abandon_autorelease=abandon_autorelease,
    )


async def stage_changes(
    local_clone: Path,
    files_to_commit: str | list[str],
) -> None:
    if isinstance(files_to_commit, str):
        files_to_commit = [files_to_commit]

    for file in files_to_commit:
        logger.info(f"Staging: {file}")
        exit_code, _, stderr = await run_subprocess(["git", "add", "--all", file], cwd=local_clone)
        # for the case agent already staged deleted file which leads to error
        if exit_code != 0:
            logger.warning(f"Failed to stage {file}: {stderr}")


async def commit_and_push(
    local_clone: Path,
    commit_message: str,
    fork_url: str,
    update_branch: str,
    available_tools: list[Tool],
    commit_only: bool = False,
    allow_empty: bool = False,
) -> bool:
    """
    Commits the changes to the local clone.

    Returns:
        - str: The URL of the merge request if it was created successfully
        - bool: True if the merge request was created, False otherwise (i.e. MR was reused)
    """
    if not allow_empty:
        # Check if any files are staged before committing, if none, bail
        exit_code, _, _ = await run_subprocess(
            ["git", "diff", "--cached", "--quiet"],
            cwd=local_clone,
        )
        # 1 = staged, 0 = none staged
        if exit_code == 0:
            logger.info("No files staged for commit, halting.")
            raise RuntimeError("No files staged for commit, halting.")
    commit_cmd = ["git", "commit"]
    if allow_empty:
        commit_cmd.append("--allow-empty")
    commit_cmd.extend(["-m", commit_message])
    await check_subprocess(commit_cmd, cwd=local_clone)
    if commit_only:
        return False
    await run_tool(
        "push_to_remote_repository",
        repository=fork_url,
        clone_path=str(local_clone),
        branch=update_branch,
        force=True,
        available_tools=available_tools,
    )
    return True


async def commit_push_and_open_mr(
    local_clone: Path,
    commit_message: str,
    fork_url: str,
    dist_git_branch: str,
    update_branch: str,
    mr_title: str,
    mr_description: str,
    available_tools: list[Tool],
    commit_only: bool = False,
    allow_empty: bool = False,
    labels: list[str] | None = None,
) -> tuple[str | None, bool]:
    """
    Commits the changes to the local clone and opens a merge request.

    Returns:
        - str: The URL of the merge request if it was created successfully
        - bool: True if the merge request was created, False otherwise (i.e. MR was reused)
    """
    if not await commit_and_push(
        local_clone,
        commit_message,
        fork_url,
        update_branch,
        available_tools,
        commit_only,
        allow_empty,
    ):
        return None, False
    result = await run_tool(
        "open_merge_request",
        fork_url=fork_url,
        title=mr_title,
        description=mr_description,
        target=dist_git_branch,
        source=update_branch,
        available_tools=available_tools,
    )
    mr = OpenMergeRequestResult.model_validate(result)
    if mr.url and labels:
        try:
            await run_tool(
                "add_merge_request_labels",
                merge_request_url=mr.url,
                labels=labels,
                available_tools=available_tools,
            )
        except Exception as e:
            logger.warning(f"Failed to add labels {labels} to MR {mr.url}: {e}")
    return mr.url, mr.is_new_mr


async def comment_in_jira(
    jira_issue: str,
    agent_type: str,
    comment_text: str,
    available_tools: list[Tool],
    is_error: bool = False,
    user_triggered: bool = False,
) -> None:
    # Default is silent: error comments are only posted on user-triggered runs.
    # A maintainer who didn't ask for processing should not be spammed with
    # error notifications; if they want to see them, they add ymir_todo.
    if is_error and not user_triggered:
        logger.info(f"Skipping Jira error comment for {jira_issue} (not user-triggered)")
        return

    await run_tool(
        "add_jira_comment",
        issue_key=jira_issue,
        comment=JIRA_COMMENT_TEMPLATE.substitute(AGENT_TYPE=agent_type, JIRA_COMMENT=comment_text),
        private=True,
        available_tools=available_tools,
    )


async def post_user_ack_once(
    task: Task,
    jira_issue: str,
    agent_type: str,
    comment_text: str,
    user_triggered: bool,
    dry_run: bool,
) -> None:
    """Post a user-triggered acknowledgement comment to Jira exactly once per task.

    Tracks delivery via ``task.metadata['ack_posted']`` so a re-queued retry
    of the same task sees it as already delivered and skips the post. The
    flag is only set after ``comment_in_jira`` returns successfully, so a
    failed post still leaves the next retry free to try again.
    """
    if not user_triggered or dry_run:
        return
    if task.metadata.get("ack_posted"):
        return
    try:
        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            await comment_in_jira(
                jira_issue=jira_issue,
                agent_type=agent_type,
                comment_text=comment_text,
                available_tools=gateway_tools,
                user_triggered=True,
            )
        task.metadata["ack_posted"] = True
    except Exception as e:
        logger.warning(f"Failed to post user-triggered ack comment for {jira_issue}: {e}")


async def comment_in_mr(
    merge_request_url: str,
    comment_text: str,
    available_tools: list[Tool],
) -> None:
    await run_tool(
        "add_merge_request_comment",
        merge_request_url=merge_request_url,
        comment=comment_text,
        available_tools=available_tools,
    )


async def change_jira_status(
    jira_issue: str,
    status: str,
    available_tools: list[Tool],
) -> None:
    if os.getenv("JIRA_ALLOW_STATUS_CHANGES", "false").lower() != "true":
        logger.info(
            f"JIRA_ALLOW_STATUS_CHANGES is not set; skipping status change of {jira_issue} to {status!r}"
        )
        return
    await run_tool(
        "change_jira_status",
        issue_key=jira_issue,
        status=status,
        available_tools=available_tools,
    )


async def get_jira_labels(jira_issue: str) -> list[str]:
    try:
        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            details = await run_tool(
                "get_jira_details",
                issue_key=jira_issue,
                available_tools=gateway_tools,
            )
            return details.get("fields", {}).get("labels", [])
    except Exception as e:
        logger.warning(f"Failed to get labels for {jira_issue}: {e}")
        return []


# Intermediate "_failed" labels (transient retry-state) are suppressed for
# non-user-triggered runs — they're noise for maintainers and a retry will
# follow. Terminal "_errored" labels are kept regardless: they are the only
# dedup anchor left after retries are exhausted, so suppressing them would let
# the next fetcher sweep re-enqueue the same issue forever.
_INTERMEDIATE_LABEL_SUFFIXES = ("_failed",)


_CRITICAL_WRITE_MAX_ATTEMPTS = 3


async def set_jira_labels(
    jira_issue: str,
    labels_to_add: list[str] | None = None,
    labels_to_remove: list[str] | None = None,
    dry_run: bool = False,
    user_triggered: bool = False,
    critical: bool = False,
) -> None:
    """Edit labels on a Jira issue.

    When ``critical=True``, the write is treated as load-bearing for dedup:
    failures are retried with exponential backoff and re-raised on permanent
    failure so the caller can take recovery action (typically: re-queue the
    task and abort processing). When ``critical=False`` (default), failures
    are logged and swallowed.
    """
    if dry_run or os.getenv("JIRA_DRY_RUN", "false").lower() == "true":
        logger.info(f"Dry run, not updating labels for {jira_issue}")
        return

    if not labels_to_add and not labels_to_remove:
        return

    if not user_triggered:
        original_count = len(labels_to_add or [])
        labels_to_add = [
            label for label in (labels_to_add or []) if not label.endswith(_INTERMEDIATE_LABEL_SUFFIXES)
        ]
        if len(labels_to_add) != original_count:
            logger.info(f"Skipping intermediate failure labels for {jira_issue} (not user-triggered)")
        if not labels_to_add and not (labels_to_remove or []):
            return

    max_attempts = _CRITICAL_WRITE_MAX_ATTEMPTS if critical else 1
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
                await run_tool(
                    "edit_jira_labels",
                    issue_key=jira_issue,
                    labels_to_add=labels_to_add or [],
                    labels_to_remove=labels_to_remove or [],
                    available_tools=gateway_tools,
                )
            return
        except Exception as e:
            last_exc = e
            if not critical:
                logger.warning(f"Failed to update labels for {jira_issue}: {e}")
                return
            if attempt < max_attempts:
                backoff_seconds = 2 ** (attempt - 1)
                logger.warning(
                    f"Critical label write failed for {jira_issue} "
                    f"(attempt {attempt}/{max_attempts}): {e}; "
                    f"retrying in {backoff_seconds}s"
                )
                await asyncio.sleep(backoff_seconds)

    logger.error(f"Critical label write for {jira_issue} failed after {max_attempts} attempts: {last_exc}")
    raise last_exc  # type: ignore[misc]


async def cache_mr_metadata(
    redis_conn,
    log_output: LogOutputSchema,
    operation_type: str,
    package: str,
    details: str,
) -> LogOutputSchema:
    """
    Cache MR metadata for sharing across streams.

    Returns cached metadata if it exists, otherwise stores and returns the provided one.

    Args:
        redis_conn: Redis client connection
        operation_type: Type of operation ("backport" or "rebase")
        package: Package name
        details: Operation-specific identifier (upstream_fix URL for backport, version for rebase)
        log_output: LogOutputSchema to store if not cached

    Returns:
        LogOutputSchema: With cached title if available, otherwise original title
    """
    # As the upstream_fix URL can be quite long, use only the hash
    details_hash = hashlib.sha256(details.encode()).hexdigest()[:16]
    cache_key = f"mr_metadata:{operation_type}:{package}:{details_hash}"

    # Try to get previously cached metadata
    cached = await redis_conn.get(cache_key)
    if cached is not None:
        logger.info(f"MR metadata cache HIT for {operation_type}/{package}/{details} (key: {cache_key})")
        try:
            metadata = CachedMRMetadata.model_validate_json(cached)
            # Override the title by value stored in the cache
            return LogOutputSchema(title=metadata.title, description=log_output.description)
        except ValueError as e:
            logger.warning(f"Error validating cached MR metadata for key {cache_key}: {e}")

    # Store new metadata on cache miss or validation error
    metadata = CachedMRMetadata(
        operation_type=operation_type,
        title=log_output.title,
        package=package,
        details=details,
    )
    await redis_conn.set(cache_key, metadata.model_dump_json())
    logger.info(f"MR metadata cache stored for {operation_type}/{package}/{details} (key: {cache_key})")

    return log_output


def get_unpacked_sources(local_clone: Path, package: str) -> Path:
    """
    Get a path to the root of extracted archive directory tree (referenced as TLD
    in RPM documentation) for a given package.
    """
    with Specfile(local_clone / f"{package}.spec") as spec:
        name = spec.expand("%{name}")
        version = spec.expand("%{version}")
        buildsubdir = spec.expand("%{buildsubdir}")
    if "/" in buildsubdir:
        # When %setup -n uses a nested path (e.g. libexpat-R_2_6_4/expat),
        # use the archive root because some specs apply patches at that level
        # via pushd/popd.  More details: https://github.com/packit/jotnar/issues/217
        buildsubdir = buildsubdir.split("/")[0]

    # RPM 4.20+ uses a per-build directory named %{NAME}-%{VERSION}-build
    per_build_dir = local_clone / f"{name}-{version}-build"
    sources_dir = per_build_dir / buildsubdir
    if sources_dir.is_dir():
        return sources_dir

    # Older RPM versions unpack directly under _builddir
    sources_dir = local_clone / buildsubdir
    if sources_dir.is_dir():
        return sources_dir

    raise ValueError(f"Unpacked source directory does not exist: {sources_dir}")


async def _fallback_extract_sources(local_clone: Path, package: str) -> Path:
    """
    Fallback when centpkg/rhpkg prep fails: extract the primary source
    archive using Source0 from the spec file.
    """
    try:
        with Specfile(local_clone / f"{package}.spec") as spec, spec.sources() as sources:
            if not sources:
                raise ValueError(f"No sources defined in {package}.spec")
            archive = local_clone / sources[0].expanded_filename
            if not archive.is_file():
                raise ValueError(f"Source0 '{sources[0].expanded_filename}' not found on disk")
    except Exception as e:
        raise ValueError(f"Could not determine source archive for {package}: {e}") from e
    logger.info(f"Using Source0 from spec: {archive.name}")

    extract_dir = local_clone / "_extracted"
    extract_dir.mkdir(exist_ok=True)

    cmd = ["/usr/lib/rpm/rpmuncompress", "-x", str(archive)]
    logger.info(f"Extracting {archive.name} to {extract_dir}")

    exit_code, _, stderr = await run_subprocess(cmd, cwd=extract_dir)
    if exit_code != 0:
        raise ValueError(f"Failed to extract {archive.name}: {stderr}")

    subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
    if len(subdirs) == 1:
        return subdirs[0]
    return extract_dir


async def clone_and_prep_sources(
    package: str,
    dist_git_branch: str,
    available_tools: list[Tool],
    jira_issue: str,
    ref: str | None = None,
) -> tuple[Path, Path, bool]:
    """
    Clone dist-git repo and run centpkg/rhpkg sources + prep.
    Returns (local_clone, unpacked_sources, prep_succeeded).
    Read-only: no fork, no push — just for source analysis.

    Falls back to manual archive extraction if prep fails (e.g. missing
    language-specific RPM macros). When using the fallback, downstream
    patches are NOT applied — the source is pristine upstream.

    When *ref* is provided (a commit SHA), the repo is cloned with all
    refs and that specific commit is checked out.  This is used when the
    target branch does not exist yet but we know the base commit from Koji.
    """
    if not jira_issue or Path(jira_issue).is_absolute() or ".." in jira_issue:
        raise ValueError(f"Invalid jira_issue: {jira_issue}")
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / "applicability" / jira_issue
    if working_dir.is_dir():
        shutil.rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)
    local_clone = working_dir / package

    namespace = "centos-stream" if is_cs_branch(dist_git_branch) else "rhel"
    repository = f"https://gitlab.com/redhat/{namespace}/rpms/{package}"
    if ref:
        await run_tool(
            "clone_repository",
            repository=repository,
            clone_path=str(local_clone),
            available_tools=available_tools,
        )
        exit_code, _, stderr = await run_subprocess(["git", "checkout", ref], cwd=local_clone)
        if exit_code != 0:
            raise RuntimeError(f"Failed to checkout ref {ref}: {stderr}")
    else:
        await run_tool(
            "clone_repository",
            repository=repository,
            branch=dist_git_branch,
            clone_path=str(local_clone),
            available_tools=available_tools,
        )

    await run_tool(
        "download_sources",
        dist_git_path=str(local_clone),
        package=package,
        dist_git_branch=dist_git_branch,
        available_tools=available_tools,
    )

    # Run prep locally rather than via MCP gateway: the agent container is
    # RHEL-based so rpmbuild evaluates %prep macros correctly, whereas the
    # MCP gateway runs Fedora and would expand them differently.
    if is_cs_branch(dist_git_branch):
        pkg_cmd = [
            "centpkg",
            f"--name={package}",
            "--namespace=rpms",
            f"--release={dist_git_branch}",
        ]
    else:
        pkg_cmd = [
            "rhpkg",
            f"--name={package}",
            "--namespace=rpms",
            f"--release={dist_git_branch}",
            "--offline",
            "--released",
        ]
    try:
        exit_code, _, stderr = await run_subprocess([*pkg_cmd, "prep"], cwd=local_clone)
    except FileNotFoundError:
        logger.warning(
            f"prep failed for {package}: {pkg_cmd[0]} is not installed, falling back to manual extraction"
        )
        unpacked = await _fallback_extract_sources(local_clone, package)
        return local_clone, unpacked, False

    if exit_code == 0:
        unpacked = get_unpacked_sources(local_clone, package)
        return local_clone, unpacked, True

    logger.warning(f"prep failed for {package}, falling back to manual extraction: {stderr}")
    unpacked = await _fallback_extract_sources(local_clone, package)
    return local_clone, unpacked, False
