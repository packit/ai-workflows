import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import yaml
from beeai_framework.errors import FrameworkError
from beeai_framework.tools import Tool
from specfile import Specfile

from ymir.agents.constants import BRANCH_PREFIX, JIRA_COMMENT_TEMPLATE, trace_viewer_issue_url
from ymir.agents.utils import check_subprocess, mcp_tools, run_subprocess, run_tool
from ymir.common.base_utils import fix_await, is_cs_branch, is_modular_branch, resolve_dist_git_namespace
from ymir.common.config import load_rhel_config
from ymir.common.constants import RedisQueues
from ymir.common.merge_queue import (  # noqa: F401 — re-exported for agents and tests
    _CONSOLIDATION_HASH_KEY,
    SubmitResult,
    _consolidation_field_key,
    complete_job,
    pick_next_job,
    refresh_active_heartbeat,
    requeue_active_job,
    submit_merge_job,
    sweep_stale_active_jobs,
)
from ymir.common.models import (
    CachedMRMetadata,
    ErrorData,
    ErrorListEntry,
    MergeRequestDetails,
    OpenMergeRequestResult,
    PackageConsolidationConfig,
    PackageReleaseBumpingConfig,
    PackageReproducerConfig,
    Task,
)
from ymir.common.reproducer_lock import resolve_clone_root
from ymir.common.utils import get_all_sources, get_latest_candidate_build, get_latest_z_pending_build
from ymir.common.version_utils import (
    construct_internal_branch_name,
    is_older_zstream,
    parse_rhel_version,
    parse_zstream_branch_name,
)
from ymir.tools.privileged.utils import ACTIVE_WORKSPACE_MARKER, APPLICABILITY_DIR, MERGE_REQUESTS_DIR
from ymir.tools.unprivileged.specfile import UpdateReleaseTool
from ymir.tools.unprivileged.wicked_git import RunPackagePrepTool

logger = logging.getLogger(__name__)


class ZStreamBranchStaleError(FrameworkError):
    """Raised when a z-stream branch is behind the latest Brew build."""

    def __init__(self, package: str, branch: str, build_ref: str, branch_head: str):
        self.package = package
        self.branch = branch
        self.build_ref = build_ref
        self.branch_head = branch_head
        super().__init__(
            f"Z-stream branch {branch} for {package} is out of sync with compose. "
            f"Branch HEAD ({branch_head[:12]}) does not contain the latest "
            f"build ref ({build_ref[:12]}). "
            "The branch maintainer needs to update it before Ymir can proceed. "
            "Please fix the branch and re-trigger by removing all ymir_ labels "
            "and adding ymir_todo.",
            is_retryable=False,
        )


async def _check_zstream_branch_consistency(package: str, dist_git_branch: str, local_clone: Path) -> None:
    """Verify that a z-stream branch contains the latest Brew build's source commit.

    Raises ZStreamBranchStaleError if the branch is behind.
    Logs a warning and returns normally if the check cannot be performed
    (e.g. Brew unreachable, no builds in tag).
    """
    if not parse_zstream_branch_name(dist_git_branch):
        return

    try:
        if await is_older_zstream(dist_git_branch):
            _, build_source_ref = await get_latest_z_pending_build(package, dist_git_branch)
        else:
            _, build_source_ref = await get_latest_candidate_build(package, dist_git_branch)
    except Exception as e:
        logger.warning(
            f"Could not query Brew for z-stream branch consistency ({package}/{dist_git_branch}): {e}"
        )
        return

    exit_code, _, stderr = await run_subprocess(
        ["git", "merge-base", "--is-ancestor", build_source_ref, "HEAD"],
        cwd=local_clone,
    )
    if exit_code == 0:
        return

    # exit 1 = not ancestor; exit 128 = "not a valid commit" (ref not in repo).
    # Both mean the branch is stale. Any other non-zero is an unexpected git
    # failure — soft-fail so we don't post a misleading maintainer message.
    if exit_code not in (1, 128):
        logger.warning(
            f"Unexpected git merge-base exit {exit_code} checking z-stream "
            f"consistency ({package}/{dist_git_branch}): {stderr}"
        )
        return

    _, head_stdout, _ = await run_subprocess(["git", "rev-parse", "HEAD"], cwd=local_clone)
    raise ZStreamBranchStaleError(package, dist_git_branch, build_source_ref, (head_stdout or "").strip())


async def handle_zstream_branch_stale_error(
    exc: ZStreamBranchStaleError,
    *,
    jira_issues: list[str],
    primary_jira_issue: str,
    agent_type: str,
    errored_label: str,
    triaged_label: str,
    dry_run: bool,
    user_triggered: bool,
    redis_conn,
    task: Task | None = None,
    queue: str | None = None,
) -> None:
    """Terminal handling for a stale z-stream branch: label, comment, ERROR_LIST.

    Does not re-queue. Always posts the Jira comment (unless dry_run) because
    only the maintainer can fix the branch.
    """
    issues = list(dict.fromkeys(jira_issues))
    logger.error(f"Stale z-stream branch for {primary_jira_issue}: {exc}")
    for issue_key in issues:
        try:
            await set_jira_labels(
                jira_issue=issue_key,
                labels_to_add=[errored_label],
                labels_to_remove=[triaged_label],
                dry_run=dry_run,
                user_triggered=user_triggered,
            )
        except Exception as label_error:
            logger.warning(f"Failed to set labels on {issue_key}: {label_error}")
    if not dry_run:
        try:
            async with mcp_tools(
                os.environ["MCP_GATEWAY_URL"],
                call_meta={"jira_issue": primary_jira_issue},
            ) as gateway_tools:
                for issue_key in issues:
                    try:
                        await post_terminal_error_comment(
                            jira_issue=issue_key,
                            agent_type=agent_type,
                            comment_text=str(exc),
                            available_tools=gateway_tools,
                        )
                    except Exception as comment_error:
                        logger.warning(
                            f"Failed to post stale-branch comment for {issue_key}: {comment_error}"
                        )
        except Exception as gateway_error:
            logger.warning(f"Failed to post stale-branch comment: {gateway_error}")
    error_id = await fix_await(redis_conn.incr(RedisQueues.ERROR_ID_COUNTER.value))
    entry = ErrorListEntry(
        error_id=error_id,
        queue=queue,
        task=task,
        error=ErrorData(details=str(exc), jira_issue=primary_jira_issue),
    )
    await fix_await(redis_conn.lpush(RedisQueues.ERROR_LIST.value, entry.model_dump_json()))


async def needs_zstream_target_label(dist_git_branch: str, fix_version: str | None) -> bool:
    """Check if the fix targets a z-stream on an active CentOS Stream.

    Maintenance streams (e.g. c8s / RHEL 8) are excluded — all builds there are
    z-stream by default, so the label would add no information.
    """
    if not fix_version or not is_cs_branch(dist_git_branch):
        return False
    parsed = parse_rhel_version(fix_version)
    if not parsed or not parsed[2]:
        return False

    config = await load_rhel_config()
    major = parsed[0]
    y_streams = config.get("current_y_streams", {})
    return major in y_streams


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


def _force_rmtree(path: Path | str) -> None:
    """Best-effort removal of a directory tree.

    In containerised setups the MCP gateway (running as a different UID)
    creates files that the agent container cannot delete.  We try
    ``rm -rf`` and tolerate partial failures — the subsequent clone will
    reinitialise the git state over any leftover files.
    """
    result = subprocess.run(["rm", "-rf", str(path)], capture_output=True)  # noqa: S603, S607
    if result.returncode != 0:
        logger.warning(
            "Could not fully remove %s (exit %d): %s — proceeding anyway",
            path,
            result.returncode,
            result.stderr.decode().strip(),
        )


async def fork_and_prepare_dist_git(
    jira_issue: str,
    package: str,
    dist_git_branch: str,
    available_tools: list[Tool],
    agent_type: str,
    workspace_id: UUID | None = None,
    with_fedora: bool = False,
    dist_git_namespace: str | None = None,
) -> tuple[Path, str, str, Path | None, str | None]:
    if not jira_issue or Path(jira_issue).is_absolute() or ".." in jira_issue:
        raise ValueError(f"Invalid jira_issue: {jira_issue}")
    workspace_id = workspace_id or uuid4()
    # A workflow invocation owns only its unique workspace. Queue retries and
    # concurrent direct runs cannot delete each other's checkout.
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / agent_type / jira_issue / str(workspace_id)
    if working_dir.is_dir():
        _force_rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)
    (working_dir / ACTIVE_WORKSPACE_MARKER).touch()
    namespace = resolve_dist_git_namespace(dist_git_branch, dist_git_namespace)
    repository = f"https://gitlab.com/redhat/{namespace}/rpms/{package}"
    fork_url = await run_tool("fork_repository", repository=repository, available_tools=available_tools)
    local_clone = working_dir / package
    # create_zstream_branch only applies to plain internal rhel-X.Y[.0] branches;
    # modular stream-* branches already exist in the rhel project.
    zstream_branch_created = None
    if not is_cs_branch(dist_git_branch) and not is_modular_branch(dist_git_branch):
        result = await run_tool(
            "create_zstream_branch",
            package=package,
            branch=dist_git_branch,
            available_tools=available_tools,
        )
        if "already exists" not in result:
            zstream_branch_created = result
    if await is_older_zstream(dist_git_branch):
        await run_tool(
            "clone_repository",
            repository=repository,
            clone_path=str(local_clone),
            available_tools=available_tools,
        )
        await check_subprocess(["git", "checkout", dist_git_branch], cwd=local_clone)
    else:
        await run_tool(
            "clone_repository",
            repository=repository,
            branch=dist_git_branch,
            clone_path=str(local_clone),
            available_tools=available_tools,
        )
    await _check_zstream_branch_consistency(package, dist_git_branch, local_clone)
    update_branch = f"{BRANCH_PREFIX}-{jira_issue}"
    await check_subprocess(["git", "checkout", "-B", update_branch], cwd=local_clone)
    fedora_clone = None
    if with_fedora:
        fedora_clone = working_dir / f"{package}-fedora"
        if not await _clone_fedora_dist_git(package, fedora_clone):
            fedora_clone = None
    return local_clone, update_branch, fork_url, fedora_clone, zstream_branch_created


async def find_leading_zstream_branch(dist_git_branch: str) -> str | None:
    """Return the current (leading) z-stream branch if it is higher than *dist_git_branch*.

    Looks up the leading z-stream for the same RHEL major version from
    rhel-config.json and returns its dist-git branch name, or ``None`` when
    the branch is already the leading z-stream (or not a z-stream at all).
    """
    parsed = parse_zstream_branch_name(dist_git_branch)
    if not parsed:
        return None
    major, minor_str = parsed

    from ymir.common.config import load_rhel_config

    config = await load_rhel_config()
    current_zstream = (config.get("current_z_streams") or {}).get(major)
    if not current_zstream:
        return None
    current_parsed = parse_rhel_version(current_zstream)
    if not current_parsed:
        return None
    current_minor = int(current_parsed[1])
    if current_minor <= int(minor_str):
        return None
    return construct_internal_branch_name(major, current_parsed[1])


async def prepare_dist_git_from_merge_request(
    merge_request_url: str,
    available_tools: list[Tool],
    with_fedora: bool = False,
) -> tuple[Path, MergeRequestDetails, Path | None]:
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / MERGE_REQUESTS_DIR
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
        fedora_clone = working_dir / f"{package}-fedora-{local_clone.name}"
        if not await _clone_fedora_dist_git(package, fedora_clone):
            fedora_clone = None
    return local_clone, details, fedora_clone


class InvalidReleaseBumpingConfigError(Exception):
    """Raised when ymir.yaml exists but the release_bumping section cannot be parsed."""


async def fetch_release_bumping_config(
    package: str,
    available_tools: list,
) -> PackageReleaseBumpingConfig:
    """Fetch the release bumping config from the per-package rules repo.

    Reads the ``release_bumping`` section from ``ymir.yaml`` at
    ``gitlab.com/redhat/centos-stream/rules/<package>``.
    Returns the default config (plain %autorelease/Y-stream bumping) when the
    file is absent or has no ``release_bumping`` key.

    Raises:
        InvalidReleaseBumpingConfigError: When the file exists but the
            ``release_bumping`` section does not conform to the expected schema.

    Args:
        package: RPM package name.
        available_tools: MCP gateway tools (must include ``get_maintainer_rules``).

    Returns:
        Parsed release bumping config.
    """
    try:
        raw = await run_tool(
            "get_maintainer_rules",
            package=package,
            file_path="ymir.yaml",
            available_tools=available_tools,
        )
    except Exception as e:
        logger.warning("Failed to fetch ymir.yaml for %s: %s", package, e)
        return PackageReleaseBumpingConfig()

    if "not found" in raw.lower():
        return PackageReleaseBumpingConfig()

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise InvalidReleaseBumpingConfigError(f"ymir.yaml for {package} is not valid YAML: {e}") from e

    if not isinstance(data, dict) or "release_bumping" not in data:
        return PackageReleaseBumpingConfig()

    try:
        return PackageReleaseBumpingConfig.model_validate(data["release_bumping"])
    except Exception as e:
        raise InvalidReleaseBumpingConfigError(
            f"ymir.yaml release_bumping section for {package} is malformed: {e}"
        ) from e


async def update_release(
    local_clone: Path,
    package: str,
    dist_git_branch: str,
    rebase: bool,
    available_tools: list,
) -> None:
    config = await fetch_release_bumping_config(package, available_tools)
    await run_tool(
        UpdateReleaseTool(options={"working_directory": local_clone}),
        spec=f"{package}.spec",
        package=package,
        dist_git_branch=dist_git_branch,
        rebase=rebase,
        abandon_autorelease=config.abandon_autorelease,
        treat_maintenance_rhel_as_zstream=config.treat_maintenance_rhel_as_zstream,
        disregard_zstream_nvr_policy=config.disregard_zstream_nvr_policy,
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
    await commit_changes(local_clone, commit_message, allow_empty)
    if commit_only:
        return False
    await push_changes(local_clone, fork_url, update_branch, available_tools)
    return True


async def commit_changes(
    local_clone: Path,
    commit_message: str,
    allow_empty: bool = False,
) -> str:
    """Create a local commit and return its full object ID."""
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
    commit_sha, _ = await check_subprocess(["git", "rev-parse", "HEAD"], cwd=local_clone)
    return commit_sha.strip()


async def push_changes(
    local_clone: Path,
    fork_url: str,
    update_branch: str,
    available_tools: list[Tool],
) -> None:
    """Push an already-created update commit to the package fork."""
    await run_tool(
        "push_to_remote_repository",
        repository=fork_url,
        clone_path=str(local_clone),
        branch=update_branch,
        force=True,
        available_tools=available_tools,
    )


async def request_mr_reviews(
    package: str,
    dist_git_branch: str,
    mr_url: str,
    available_tools: list[Tool],
) -> None:
    """Best-effort reviewer assignment — logs warnings but never raises."""
    if os.getenv("ASSIGN_MR_REVIEWERS", "false").lower() != "true":
        return
    try:
        reviewer_ids = await run_tool(
            "resolve_reviewers",
            package=package,
            dist_git_branch=dist_git_branch,
            available_tools=available_tools,
        )
        if not reviewer_ids:
            logger.info("No reviewers resolved for %s (%s)", package, dist_git_branch)
            return
        await run_tool(
            "set_merge_request_reviewers",
            merge_request_url=mr_url,
            reviewer_ids=reviewer_ids,
            available_tools=available_tools,
        )
        logger.info("Assigned reviewers %s to MR %s", reviewer_ids, mr_url)
    except Exception as e:
        logger.warning("Failed to assign reviewers to MR %s: %s", mr_url, e)


async def request_mr_qe_reviews(
    package: str,
    dist_git_branch: str,
    mr_url: str,
    available_tools: list[Tool],
) -> None:
    """Best-effort QE reviewer assignment — logs warnings but never raises."""
    if os.getenv("ASSIGN_MR_REVIEWERS", "false").lower() != "true":
        return
    try:
        reviewer_ids = await run_tool(
            "resolve_qe_reviewers",
            package=package,
            dist_git_branch=dist_git_branch,
            available_tools=available_tools,
        )
        if not reviewer_ids:
            logger.info("No QE reviewers resolved for %s (%s)", package, dist_git_branch)
            return
        await run_tool(
            "set_merge_request_reviewers",
            merge_request_url=mr_url,
            reviewer_ids=reviewer_ids,
            available_tools=available_tools,
        )
        logger.info("Assigned QE reviewers %s to MR %s", reviewer_ids, mr_url)
    except Exception as e:
        logger.warning("Failed to assign QE reviewers to MR %s: %s", mr_url, e)


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
    package: str | None = None,
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
    return await open_update_merge_request(
        fork_url=fork_url,
        dist_git_branch=dist_git_branch,
        update_branch=update_branch,
        mr_title=mr_title,
        mr_description=mr_description,
        available_tools=available_tools,
        labels=labels,
        package=package,
    )


async def open_update_merge_request(
    fork_url: str,
    dist_git_branch: str,
    update_branch: str,
    mr_title: str,
    mr_description: str,
    available_tools: list[Tool],
    labels: list[str] | None = None,
    package: str | None = None,
) -> tuple[str | None, bool]:
    """Open or reuse the MR for an update branch that is already pushed."""
    tool_kwargs = {
        "fork_url": fork_url,
        "title": mr_title,
        "description": mr_description,
        "target": dist_git_branch,
        "source": update_branch,
    }
    if labels:
        tool_kwargs["labels"] = labels
    result = await run_tool(
        "open_merge_request",
        **tool_kwargs,
        available_tools=available_tools,
    )
    mr = OpenMergeRequestResult.model_validate(result)
    if not mr.is_new_mr and mr.url and labels:
        try:
            await run_tool(
                "add_merge_request_labels",
                merge_request_url=mr.url,
                labels=labels,
                available_tools=available_tools,
            )
        except Exception as e:
            logger.warning(f"Failed to add labels {labels} to MR {mr.url}: {e}")
    if mr.url and mr.is_new_mr and package:
        await request_mr_reviews(package, dist_git_branch, mr.url, available_tools)
    return mr.url, mr.is_new_mr


async def comment_in_jira(
    jira_issue: str,
    agent_type: str,
    comment_text: str,
    available_tools: list[Tool],
    is_error: bool = False,
    user_triggered: bool = False,
) -> None:
    # Mid-workflow errors (e.g. consolidation failures in backport/rebase) are
    # trigger-gated here; crash-based and resolution-based terminal errors
    # bypass this via post_terminal_error_comment() after retries are exhausted.
    if is_error and not user_triggered:
        logger.info(f"Skipping Jira error comment for {jira_issue} (not user-triggered)")
        return

    await _post_jira_comment(
        jira_issue=jira_issue,
        agent_type=agent_type,
        comment_text=comment_text,
        available_tools=available_tools,
        is_error=is_error,
    )


async def post_terminal_error_comment(
    jira_issue: str,
    agent_type: str,
    comment_text: str,
    available_tools: list[Tool],
) -> None:
    """Post an error comment for a terminal failure, regardless of trigger."""
    await _post_jira_comment(
        jira_issue=jira_issue,
        agent_type=agent_type,
        comment_text=comment_text,
        available_tools=available_tools,
        is_error=True,
    )


async def _post_jira_comment(
    jira_issue: str,
    agent_type: str,
    comment_text: str,
    available_tools: list[Tool],
    is_error: bool,
) -> None:
    trace_server_url = trace_viewer_issue_url(jira_issue)
    if is_error and trace_server_url:
        comment_text = (
            f"{comment_text}\n\nSee the [Ymir execution trace|{trace_server_url}] for additional details."
        )

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


async def get_jira_issue_metadata(jira_issue: str) -> tuple[list[str], str | None]:
    """Fetch labels and status for a Jira issue in a single API call."""
    try:
        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            details = await run_tool(
                "get_jira_details",
                issue_key=jira_issue,
                available_tools=gateway_tools,
            )
            labels = details.get("fields", {}).get("labels", [])
            status = details.get("fields", {}).get("status", {}).get("name")
            return labels, status
    except Exception as e:
        logger.warning(f"Failed to get metadata for {jira_issue}: {e}")
        return [], None


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


_CANONICAL_MR_TITLE_TTL_SECONDS = 30 * 24 * 60 * 60
_CVE_ID_RE = re.compile(r"(?<![A-Z0-9])CVE-[0-9]{4}-[0-9]{4,}(?![A-Z0-9])", re.IGNORECASE)
_MAX_CANONICAL_TITLE_LENGTH = 255
_MAX_GENERATED_TITLE_LENGTH = 80
_MAX_CANONICAL_TITLE_REPLACEMENT_ATTEMPTS = 3
_JIRA_ISSUE_KEY_RE = re.compile(r"\b(?:RHEL|PACKIT)-\d+\b", re.IGNORECASE)
_CVE_STREAM_SUFFIX_RE = re.compile(r"\s+\[rhel-[^\]]+\]\s*$", re.IGNORECASE)
_CONDITIONAL_DELETE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_CONDITIONAL_REPLACE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
end
return nil
"""


def _cve_ids(cve_id: str | None, jira_summary: str) -> list[str]:
    """Normalize all CVE IDs found in triage metadata and the Jira summary."""
    return sorted({match.upper() for match in _CVE_ID_RE.findall(f"{cve_id or ''} {jira_summary}")})


def _parse_jira_updated(value: str | None) -> datetime | None:
    """Parse a Jira updated timestamp into UTC, treating malformed values as unavailable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _normalize_jira_updated(value: str | None) -> str | None:
    """Return a valid Jira timestamp in UTC ISO format, preserving an absent value."""
    if value is None:
        return None
    parsed = _parse_jira_updated(value)
    if parsed is None:
        raise ValueError("Jira updated timestamp must be timezone-aware ISO-8601")
    return parsed.isoformat()


def _is_newer_summary(candidate: str | None, current: str | None) -> bool:
    """Return whether two Jira updated timestamps prove the candidate is newer."""
    candidate_updated = _parse_jira_updated(candidate)
    current_updated = _parse_jira_updated(current)
    return bool(candidate_updated and current_updated and candidate_updated > current_updated)


def _validate_canonical_title(title: str, jira_issue: str) -> str:
    """Accept only bounded, single-line display data from Jira or Redis."""
    if not title or not title.strip() or len(title) > _MAX_CANONICAL_TITLE_LENGTH:
        raise ValueError(
            f"Canonical title for {jira_issue} must be 1-{_MAX_CANONICAL_TITLE_LENGTH} characters"
        )
    prohibited_categories = {"Cc", "Cf", "Zl", "Zp"}
    if any(unicodedata.category(character) in prohibited_categories for character in title):
        raise ValueError(f"Canonical title for {jira_issue} must be a single display line")
    return title


def _normalize_cve_title(title: str) -> str:
    """Remove the stream-specific suffix Jira adds to CVE summaries."""
    return _CVE_STREAM_SUFFIX_RE.sub("", title)


def _validate_generated_title(title: str, jira_issue: str) -> str:
    """Enforce the stricter title-agent output contract before publication."""
    title = _validate_canonical_title(title, jira_issue)
    if len(title) > _MAX_GENERATED_TITLE_LENGTH:
        raise ValueError(
            f"Generated title for {jira_issue} must be at most {_MAX_GENERATED_TITLE_LENGTH} characters"
        )
    if _JIRA_ISSUE_KEY_RE.search(title):
        raise ValueError(f"Generated title for {jira_issue} must not contain a Jira issue key")
    return title


async def _get_cached_canonical_metadata(
    redis_conn, cache_key: str, jira_issue: str, is_cve: bool
) -> tuple[CachedMRMetadata, str] | None:
    """Return valid cached metadata, compare-deleting malformed records only."""
    cached = await redis_conn.get(cache_key)
    if cached is None:
        return None
    try:
        metadata = CachedMRMetadata.model_validate_json(cached)
        validator = _validate_canonical_title if is_cve else _validate_generated_title
        validator(metadata.title, jira_issue)
        _normalize_jira_updated(metadata.summary_updated)
        if is_cve:
            metadata = metadata.model_copy(update={"title": _normalize_cve_title(metadata.title)})
        return metadata, cached
    except ValueError as error:
        logger.warning("Discarding invalid canonical title cache record %s: %s", cache_key, error)
        try:
            await redis_conn.eval(_CONDITIONAL_DELETE_LUA, 1, cache_key, cached)
        except Exception:
            logger.warning(
                "Could not delete invalid canonical title cache record %s", cache_key, exc_info=True
            )
    return None


def changelog_entry_count(local_clone: Path, package: str) -> int | None:
    """Return the explicit changelog entry count, or None for %autochangelog."""
    with Specfile(local_clone / f"{package}.spec") as spec:
        if spec.has_autochangelog:
            return None
        with spec.changelog() as changelog:
            return len(changelog)


def escape_rpm_changelog_text(value: str) -> str:
    """Escape RPM macro markers so changelog text is interpreted literally."""
    return value.replace("%", "%%")


def canonical_title_mentions_components(title: str, components: list[str]) -> bool:
    """Return whether a title names every rebuild dependency component."""
    normalized_title = title.casefold()
    return all(component.casefold() in normalized_title for component in components)


def ensure_canonical_changelog_title(
    local_clone: Path,
    package: str,
    title: str,
    expected_entry_count: int | None,
) -> None:
    """Replace exactly one new explicit changelog entry's descriptive line with ``title``.

    ``expected_entry_count`` is captured before the Log Agent runs. This prevents
    a failed or non-compliant Log Agent from causing an older entry to be changed.
    ``None`` denotes a %autochangelog spec, which has no explicit entry to edit.
    """
    if expected_entry_count is None:
        return
    with Specfile(local_clone / f"{package}.spec") as spec:
        if spec.has_autochangelog:
            return
        with spec.changelog() as changelog:
            if len(changelog) != expected_entry_count + 1:
                raise RuntimeError(
                    f"Expected one new changelog entry for {package}, "
                    f"found {len(changelog) - expected_entry_count}"
                )
            entry = changelog[-1]
            for index, line in enumerate(entry.content):
                is_jira_reference = re.match(r"^\s*(?:[-*]\s*)?(?:Resolves|Related):", line, re.IGNORECASE)
                if line.strip() and not is_jira_reference:
                    entry.content[index] = f"- {escape_rpm_changelog_text(title)}"
                    return
    raise RuntimeError(f"New changelog entry for {package} has no descriptive line")


def _canonical_mr_title_key(
    package: str,
    jira_summary: str,
    cve_id: str | None,
    jira_issue: str,
    clone_root: str | None = None,
) -> str:
    """Return the stable cross-stream key for an issue family."""
    cve_ids = _cve_ids(cve_id, jira_summary)
    if cve_ids:
        issue_identity = "cve:" + ",".join(cve_ids)
    else:
        issue_identity = "jira:" + (clone_root or jira_issue).strip().upper()

    identity_digest = hashlib.sha256(issue_identity.encode()).hexdigest()[:16]
    return f"mr_metadata:v2:{package}:{identity_digest}"


async def resolve_canonical_mr_title(
    redis_conn,
    *,
    package: str,
    jira_issue: str,
    jira_summary: str,
    cve_id: str | None,
    clone_root: str | None = None,
    generated_title: str | None = None,
    summary_updated: str | None = None,
    _replacement_attempts: int = 0,
) -> str:
    """Read or atomically publish the canonical title for one issue family.

    The Redis key is stable for the package/family. A matching summary digest
    reuses the record; only a newer ``summary_updated`` value from its source
    Jira issue compare-and-swaps a replacement. CVE families use the Jira
    summary with its stream-specific suffix removed; non-CVE families require
    a validated generated title.
    """
    jira_summary = _validate_canonical_title(jira_summary, jira_issue)
    summary_updated = _normalize_jira_updated(summary_updated)

    cache_key = _canonical_mr_title_key(package, jira_summary, cve_id, jira_issue, clone_root)
    cve_ids = _cve_ids(cve_id, jira_summary)
    issue_identity = ",".join(cve_ids) if cve_ids else (clone_root or jira_issue).strip().upper()
    summary_digest = hashlib.sha256(jira_summary.encode()).hexdigest()[:16]
    cached = await _get_cached_canonical_metadata(redis_conn, cache_key, jira_issue, bool(cve_ids))
    if cached is not None:
        cached_metadata, cached_value = cached
        if cached_metadata.summary_source_issue != jira_issue.upper():
            logger.info("Reused sibling canonical MR title for %s from %s", jira_issue, cache_key)
            return cached_metadata.title
        if cached_metadata.summary_digest == summary_digest or not summary_updated:
            logger.info("Reused canonical MR title for %s from %s", jira_issue, cache_key)
            return cached_metadata.title
        if cached_metadata.summary_updated and not _is_newer_summary(
            summary_updated, cached_metadata.summary_updated
        ):
            logger.info("Kept newer canonical MR title for %s from %s", jira_issue, cache_key)
            return cached_metadata.title

    title = _normalize_cve_title(jira_summary) if cve_ids else generated_title
    if title is None:
        raise ValueError(f"Non-CVE issue {jira_issue} requires a generated title on cache miss")
    title = (
        _validate_canonical_title(title, jira_issue)
        if cve_ids
        else _validate_generated_title(title, jira_issue)
    )
    metadata = CachedMRMetadata(
        title=title,
        package=package,
        issue_identity=issue_identity,
        summary_source_issue=jira_issue.upper(),
        summary_digest=summary_digest,
        summary_updated=summary_updated,
    )
    metadata_json = metadata.model_dump_json()
    if cached is None:
        created = await redis_conn.set(
            cache_key,
            metadata_json,
            nx=True,
            ex=_CANONICAL_MR_TITLE_TTL_SECONDS,
        )
    else:
        created = await redis_conn.eval(
            _CONDITIONAL_REPLACE_LUA,
            1,
            cache_key,
            cached_value,
            metadata_json,
            _CANONICAL_MR_TITLE_TTL_SECONDS,
        )
    if created:
        logger.info("Published canonical MR title for %s at %s", jira_issue, cache_key)
        return title

    cached = await _get_cached_canonical_metadata(redis_conn, cache_key, jira_issue, bool(cve_ids))
    if cached is None:
        if _replacement_attempts >= _MAX_CANONICAL_TITLE_REPLACEMENT_ATTEMPTS:
            raise RuntimeError(f"Could not publish current canonical MR title for {jira_issue}")
        return await resolve_canonical_mr_title(
            redis_conn,
            package=package,
            jira_issue=jira_issue,
            jira_summary=jira_summary,
            cve_id=cve_id,
            clone_root=clone_root,
            generated_title=generated_title,
            summary_updated=summary_updated,
            _replacement_attempts=_replacement_attempts + 1,
        )
    cached_metadata, cached_value = cached
    if (
        cached_metadata.summary_source_issue == jira_issue.upper()
        and summary_updated
        and cached_metadata.summary_digest != summary_digest
        and (
            not cached_metadata.summary_updated
            or _is_newer_summary(summary_updated, cached_metadata.summary_updated)
        )
    ):
        replaced = await redis_conn.eval(
            _CONDITIONAL_REPLACE_LUA,
            1,
            cache_key,
            cached_value,
            metadata_json,
            _CANONICAL_MR_TITLE_TTL_SECONDS,
        )
        if replaced:
            logger.info("Replaced stale canonical MR title for %s at %s", jira_issue, cache_key)
            return title
        if _replacement_attempts >= _MAX_CANONICAL_TITLE_REPLACEMENT_ATTEMPTS:
            raise RuntimeError(f"Could not publish current canonical MR title for {jira_issue}")
        return await resolve_canonical_mr_title(
            redis_conn,
            package=package,
            jira_issue=jira_issue,
            jira_summary=jira_summary,
            cve_id=cve_id,
            clone_root=clone_root,
            generated_title=generated_title,
            summary_updated=summary_updated,
            _replacement_attempts=_replacement_attempts + 1,
        )
    logger.info("Reused canonical MR title for %s from %s", jira_issue, cache_key)
    return cached_metadata.title


async def _resolve_current_canonical_mr_title(
    redis_conn,
    *,
    available_tools: list[Tool],
    package: str,
    jira_issue: str,
    cve_id: str | None,
    generate_title: Callable[[str], Awaitable[str]],
    jira_issues: list[str] | None = None,
    consolidated_cve_ids: dict[str, str | None] | None = None,
) -> str | None:
    """Resolve a canonical title only when every included issue is one family.

    A mixed-family consolidation returns ``None`` so its aggregate title is not
    read from or published to an individual family record.
    """
    details = await run_tool(
        "get_jira_details",
        issue_key=jira_issue,
        available_tools=available_tools,
    )
    jira_summary = details.get("fields", {}).get("summary")
    summary_updated = details.get("fields", {}).get("updated")
    if not isinstance(jira_summary, str):
        raise ValueError(f"Jira issue {jira_issue} has no summary")
    jira_summary = _validate_canonical_title(jira_summary, jira_issue)
    if summary_updated is not None and not isinstance(summary_updated, str):
        raise ValueError("Jira updated timestamp must be a string")
    summary_updated = _normalize_jira_updated(summary_updated)
    details_by_issue = {jira_issue.upper(): details}

    async def fetch_issue_details(issue_key: str) -> dict:
        issue_details = details_by_issue.get(issue_key.upper())
        if issue_details is None:
            issue_details = await run_tool(
                "get_jira_details",
                issue_key=issue_key,
                available_tools=available_tools,
            )
            details_by_issue[issue_key.upper()] = issue_details
        return issue_details

    async def family_identity(issue_key: str, issue_cve_id: str | None) -> tuple[str, str | None]:
        issue_details = await fetch_issue_details(issue_key)
        summary = issue_details.get("fields", {}).get("summary")
        if not isinstance(summary, str):
            raise ValueError(f"Jira issue {issue_key} has no summary")
        cve_ids = _cve_ids(issue_cve_id, summary)
        if cve_ids:
            return "cve:" + ",".join(cve_ids), None

        async def fetch_issuelinks(linked_issue_key: str) -> list[dict]:
            linked_details = await fetch_issue_details(linked_issue_key)
            return linked_details.get("fields", {}).get("issuelinks", [])

        try:
            clone_root = await resolve_clone_root(issue_key, fetch_issuelinks)
        except Exception:
            clone_root = issue_key.upper()
            logger.warning(
                "Failed to resolve clone root for %s; using issue key for canonical title",
                issue_key,
                exc_info=True,
            )
        return "jira:" + clone_root, clone_root

    all_issues = list(dict.fromkeys([jira_issue, *(jira_issues or [])]))
    consolidated_cve_ids = {key.upper(): value for key, value in (consolidated_cve_ids or {}).items()}
    families = await asyncio.gather(
        *(
            family_identity(
                issue,
                cve_id if issue.upper() == jira_issue.upper() else consolidated_cve_ids.get(issue.upper()),
            )
            for issue in all_issues
        )
    )
    if len({identity for identity, _ in families}) != 1:
        logger.info("Skipping canonical title for multi-family consolidated issues: %s", all_issues)
        return None

    family_identity_value, clone_root = families[0]
    cve_id = family_identity_value.removeprefix("cve:") if family_identity_value.startswith("cve:") else None
    cache_key = _canonical_mr_title_key(package, jira_summary, cve_id, jira_issue, clone_root)
    cached = await _get_cached_canonical_metadata(
        redis_conn, cache_key, jira_issue, bool(_cve_ids(cve_id, jira_summary))
    )
    if cached is not None:
        cached_metadata, _ = cached
        summary_digest = hashlib.sha256(jira_summary.encode()).hexdigest()[:16]
        if cached_metadata.summary_source_issue != jira_issue.upper():
            return cached_metadata.title
        if cached_metadata.summary_digest == summary_digest or not isinstance(summary_updated, str):
            return cached_metadata.title
        if cached_metadata.summary_updated and not _is_newer_summary(
            summary_updated, cached_metadata.summary_updated
        ):
            return cached_metadata.title

    generated_title = None if _cve_ids(cve_id, jira_summary) else await generate_title(jira_summary)
    return await resolve_canonical_mr_title(
        redis_conn,
        package=package,
        jira_issue=jira_issue,
        jira_summary=jira_summary,
        cve_id=cve_id,
        clone_root=clone_root,
        generated_title=generated_title,
        summary_updated=summary_updated,
    )


async def resolve_current_canonical_mr_title(
    redis_conn,
    *,
    available_tools: list[Tool],
    package: str,
    jira_issue: str,
    cve_id: str | None,
    generate_title: Callable[[str], Awaitable[str]],
    jira_issues: list[str] | None = None,
    consolidated_cve_ids: dict[str, str | None] | None = None,
) -> str | None:
    """Return canonical metadata when available, otherwise use ordinary log generation.

    Canonical title lookup is an optional enhancement: Jira, Redis, validation,
    and title-generation failures are logged and converted to ``None``.
    """
    try:
        return await _resolve_current_canonical_mr_title(
            redis_conn,
            available_tools=available_tools,
            package=package,
            jira_issue=jira_issue,
            cve_id=cve_id,
            generate_title=generate_title,
            jira_issues=jira_issues,
            consolidated_cve_ids=consolidated_cve_ids,
        )
    except Exception:
        logger.warning(
            "Could not resolve canonical title for %s; using normal log generation",
            jira_issue,
            exc_info=True,
        )
        return None


def get_unpacked_sources(local_clone: Path, package: str, builddir: Path | None = None) -> Path:
    """
    Get a path to the root of extracted archive directory tree (referenced as TLD
    in RPM documentation) for a given package. When *builddir* is given, looks
    there instead of under *local_clone*.
    """
    base = builddir or local_clone
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
    per_build_dir = base / f"{name}-{version}-build"
    sources_dir = per_build_dir / buildsubdir
    if sources_dir.is_dir():
        return sources_dir

    # Older RPM versions unpack directly under _builddir
    sources_dir = base / buildsubdir
    if sources_dir.is_dir():
        return sources_dir

    raise ValueError(f"Unpacked source directory does not exist: {sources_dir}")


async def _fallback_extract_sources(local_clone: Path, package: str) -> tuple[Path, str]:
    """
    Fallback when centpkg/rhpkg prep fails: extract the primary source
    archive using Source0 from the spec file.
    Returns (unpacked_sources, extract_dir) where extract_dir is a /tmp
    path the caller must clean up.
    """
    try:
        with Specfile(local_clone / f"{package}.spec") as spec:
            if not (sources := get_all_sources(spec)):
                raise ValueError(f"No sources defined in {package}.spec")
            archive = local_clone / sources[0].expanded_filename
            if not archive.is_file():
                raise ValueError(f"Source0 '{sources[0].expanded_filename}' not found on disk")
    except Exception as e:
        raise ValueError(f"Could not determine source archive for {package}: {e}") from e
    logger.info(f"Using Source0 from spec: {archive.name}")

    extract_dir = Path(tempfile.mkdtemp(prefix="rpmbuild-fallback-"))

    cmd = ["/usr/lib/rpm/rpmuncompress", "-x", str(archive)]
    logger.info(f"Extracting {archive.name} to {extract_dir}")

    try:
        exit_code, _, stderr = await run_subprocess(cmd, cwd=extract_dir)
        if exit_code != 0:
            raise ValueError(f"Failed to extract {archive.name}: {stderr}")
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
    except BaseException:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise
    if len(subdirs) == 1:
        return subdirs[0], str(extract_dir)
    return extract_dir, str(extract_dir)


async def clone_and_prep_sources(
    package: str,
    dist_git_branch: str,
    available_tools: list[Tool],
    jira_issue: str,
    ref: str | None = None,
    dist_git_namespace: str | None = None,
) -> tuple[Path, Path, bool, str | None]:
    """
    Clone dist-git repo and run centpkg/rhpkg sources + prep.
    Returns (local_clone, unpacked_sources, prep_succeeded, builddir).
    The caller must clean up *builddir* (a /tmp path) when done.
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
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / APPLICABILITY_DIR / jira_issue
    if working_dir.is_dir():
        _force_rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)
    local_clone = working_dir / package

    namespace = resolve_dist_git_namespace(dist_git_branch, dist_git_namespace)
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
    prep_tool = RunPackagePrepTool()
    try:
        result = await run_tool(
            prep_tool,
            dist_git_path=str(local_clone),
            package=package,
            dist_git_branch=dist_git_branch,
        )
    except BaseException:
        if builddir := (prep_tool.options or {}).get("builddir"):
            shutil.rmtree(builddir, ignore_errors=True)
        raise

    if "Prep FAILED" not in result:
        builddir = prep_tool.options.get("builddir")
        try:
            unpacked = get_unpacked_sources(
                local_clone, package, builddir=Path(builddir) if builddir else None
            )
        except BaseException:
            if builddir:
                shutil.rmtree(builddir, ignore_errors=True)
            raise
        return local_clone, unpacked, True, builddir

    logger.warning(f"prep failed for {package}, falling back to manual extraction: {result}")
    unpacked, fallback_builddir = await _fallback_extract_sources(local_clone, package)
    return local_clone, unpacked, False, fallback_builddir


class InvalidConsolidationConfigError(Exception):
    """Raised when ymir.yaml exists but the consolidation section cannot be parsed."""


class InvalidReproducerConfigError(Exception):
    """Raised when ymir.yaml exists but the reproducer section cannot be parsed."""


async def fetch_consolidation_config(
    package: str,
    available_tools: list,
) -> PackageConsolidationConfig:
    """Fetch the consolidation config from the per-package rules repo.

    Reads the ``consolidation`` section from ``ymir.yaml`` at
    ``gitlab.com/redhat/centos-stream/rules/<package>``.
    Returns the default config (merge enabled) when the file is absent
    or has no ``consolidation`` key.

    Raises:
        InvalidConsolidationConfigError: When the file exists but the
            ``consolidation`` section does not conform to the expected schema.

    Args:
        package: RPM package name.
        available_tools: MCP gateway tools (must include ``get_maintainer_rules``).

    Returns:
        Parsed consolidation config.
    """
    try:
        raw = await run_tool(
            "get_maintainer_rules",
            package=package,
            file_path="ymir.yaml",
            available_tools=available_tools,
        )
    except Exception as e:
        logger.warning("Failed to fetch ymir.yaml for %s: %s", package, e)
        return PackageConsolidationConfig()

    if "not found" in raw.lower():
        return PackageConsolidationConfig()

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise InvalidConsolidationConfigError(f"ymir.yaml for {package} is not valid YAML: {e}") from e

    if not isinstance(data, dict) or "consolidation" not in data:
        return PackageConsolidationConfig()

    try:
        return PackageConsolidationConfig.model_validate(data["consolidation"])
    except Exception as e:
        raise InvalidConsolidationConfigError(
            f"ymir.yaml consolidation section for {package} is malformed: {e}"
        ) from e


async def try_submit_consolidation_job(
    package: str,
    dist_git_branch: str,
    gateway_tools: list,
    redis_conn,
) -> None:
    """Fetch consolidation config and submit a job if enabled.

    Shared logic used by both the backport and rebuild agents after
    creating an MR.

    Raises:
        InvalidConsolidationConfigError: When ymir.yaml exists but the
            consolidation section is malformed.
    """
    config = await fetch_consolidation_config(package, gateway_tools)

    if not config.merge_mrs:
        logger.info("MR consolidation not enabled for %s, skipping", package)
        return

    if redis_conn is None:
        logger.info("No Redis connection (direct mode), skipping consolidation job submission")
        return

    submitted = await submit_merge_job(
        redis_conn,
        package,
        dist_git_branch,
        release_strategy=config.release_strategy.value,
    )
    if submitted is SubmitResult.SUBMITTED:
        logger.info("Submitted consolidation job for %s/%s", package, dist_git_branch)
    else:
        logger.info("Consolidation job already queued for %s/%s", package, dist_git_branch)


async def fetch_reproducer_config(
    package: str,
    available_tools: list,
) -> PackageReproducerConfig:
    """Fetch the reproducer config from the per-package rules repo.

    Reads the ``reproducer`` section from ``ymir.yaml`` at
    ``gitlab.com/redhat/centos-stream/rules/<package>``.
    Returns the default config (disabled) when the file is absent
    or has no ``reproducer`` key.

    Raises:
        InvalidReproducerConfigError: When the file exists but the
            ``reproducer`` section does not conform to the expected schema.

    Args:
        package: RPM package name.
        available_tools: MCP gateway tools (must include ``get_maintainer_rules``).

    Returns:
        Parsed reproducer config.
    """
    try:
        raw = await run_tool(
            "get_maintainer_rules",
            package=package,
            file_path="ymir.yaml",
            available_tools=available_tools,
        )
    except Exception as e:
        logger.warning("Failed to fetch ymir.yaml for %s: %s", package, e)
        return PackageReproducerConfig()

    if "not found" in raw.lower():
        return PackageReproducerConfig()

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise InvalidReproducerConfigError(f"ymir.yaml for {package} is not valid YAML: {e}") from e

    if not isinstance(data, dict) or "reproducer" not in data:
        return PackageReproducerConfig()

    try:
        return PackageReproducerConfig.model_validate(data["reproducer"])
    except Exception as e:
        raise InvalidReproducerConfigError(
            f"ymir.yaml reproducer section for {package} is malformed: {e}"
        ) from e
