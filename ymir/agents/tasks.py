import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import yaml
from beeai_framework.tools import Tool
from specfile import Specfile

from ymir.agents.constants import BRANCH_PREFIX, JIRA_COMMENT_TEMPLATE
from ymir.agents.utils import check_subprocess, mcp_tools, run_subprocess, run_tool
from ymir.common.base_utils import fix_await, is_cs_branch, is_modular_branch, resolve_dist_git_namespace
from ymir.common.config import load_rhel_config
from ymir.common.constants import RedisQueues
from ymir.common.merge_queue import (  # noqa: F401 — re-exported for agents and tests
    _CONSOLIDATION_HASH_KEY,
    _consolidation_field_key,
    complete_job,
    pick_next_job,
    submit_merge_job,
    sweep_stale_active_jobs,
)
from ymir.common.models import (
    CachedMRMetadata,
    ErrorData,
    ErrorListEntry,
    LogOutputSchema,
    MergeRequestDetails,
    OpenMergeRequestResult,
    PackageConsolidationConfig,
    PackageReleaseBumpingConfig,
    PackageReproducerConfig,
    Task,
)
from ymir.common.utils import get_all_sources, get_latest_candidate_build, get_latest_z_pending_build
from ymir.common.version_utils import (
    construct_internal_branch_name,
    is_older_zstream,
    parse_rhel_version,
    parse_zstream_branch_name,
)
from ymir.tools.privileged.utils import APPLICABILITY_DIR, MERGE_REQUESTS_DIR
from ymir.tools.unprivileged.specfile import UpdateReleaseTool
from ymir.tools.unprivileged.wicked_git import RunPackagePrepTool

logger = logging.getLogger(__name__)


class ZStreamBranchStaleError(Exception):
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
            f"The branch maintainer needs to update it before Ymir can proceed. "
            f"Please fix the branch and re-trigger by removing all ymir_ labels "
            f"and adding ymir_todo."
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
                        await comment_in_jira(
                            jira_issue=issue_key,
                            agent_type=agent_type,
                            comment_text=str(exc),
                            available_tools=gateway_tools,
                            is_error=True,
                            user_triggered=True,  # force-post regardless of actual trigger
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
    with_fedora: bool = False,
    dist_git_namespace: str | None = None,
) -> tuple[Path, str, str, Path | None, str | None]:
    if not jira_issue or Path(jira_issue).is_absolute() or ".." in jira_issue:
        raise ValueError(f"Invalid jira_issue: {jira_issue}")
    # Scoped by agent_type so different agent types processing the same
    # jira_issue concurrently (e.g. rebase and backport) never share a
    # working directory and can't rm -rf each other's checkout.
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / agent_type / jira_issue
    if working_dir.is_dir():
        _force_rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)
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
        with Specfile(local_clone / f"{package}.spec") as spec:
            if not (sources := get_all_sources(spec)):
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
    dist_git_namespace: str | None = None,
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
    result = await run_tool(
        RunPackagePrepTool(),
        dist_git_path=str(local_clone),
        package=package,
        dist_git_branch=dist_git_branch,
    )

    if "Prep FAILED" not in result:
        unpacked = get_unpacked_sources(local_clone, package)
        return local_clone, unpacked, True

    logger.warning(f"prep failed for {package}, falling back to manual extraction: {result}")
    unpacked = await _fallback_extract_sources(local_clone, package)
    return local_clone, unpacked, False


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
    if submitted:
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
