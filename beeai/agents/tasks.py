import itertools
import logging
import os
import shutil
from pathlib import Path
from typing import Tuple

from beeai_framework.tools import Tool

from common.utils import is_cs_branch
from constants import BRANCH_PREFIX, JIRA_COMMENT_TEMPLATE
from utils import check_subprocess, run_subprocess, run_tool, mcp_tools

logger = logging.getLogger(__name__)


async def fork_and_prepare_dist_git(
    jira_issue: str,
    package: str,
    dist_git_branch: str,
    available_tools: list[Tool],
    with_fedora: bool = False,
) -> Tuple[Path, str, str, Path | None]:
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / jira_issue
    working_dir.mkdir(parents=True, exist_ok=True)
    namespace = "centos-stream" if is_cs_branch(dist_git_branch) else "rhel"
    fork_url = await run_tool(
        "fork_repository",
        repository=f"https://gitlab.com/redhat/{namespace}/rpms/{package}",
        available_tools=available_tools,
    )
    local_clone = working_dir / package
    shutil.rmtree(local_clone, ignore_errors=True)
    await check_subprocess(
        ["git", "clone", "--single-branch", "--branch", dist_git_branch, fork_url],
        cwd=working_dir,
    )
    update_branch = f"{BRANCH_PREFIX}-{jira_issue}"
    await check_subprocess(["git", "checkout", "-B", update_branch], cwd=local_clone)
    fedora_clone = None
    if with_fedora:
        try:
            fedora_clone = working_dir / f"{package}-fedora"
            shutil.rmtree(fedora_clone, ignore_errors=True)
            await check_subprocess(
                ["git", "clone", "--single-branch", "--branch", "rawhide", f"https://src.fedoraproject.org/rpms/{package}", f"{package}-fedora"],
                cwd=working_dir,
            )
        except Exception as e:
            logger.warning(f"Failed to clone Fedora repository for {package}: {e}")
            fedora_clone = None

    return local_clone, update_branch, fork_url, fedora_clone


async def stage_changes(
    local_clone: Path,
    files_to_commit: str | list[str],
) -> None:
    if isinstance(files_to_commit, str):
        files_to_commit = [files_to_commit]
    for path in itertools.chain(*(local_clone.glob(pat) for pat in files_to_commit)):
        await check_subprocess(["git", "add", str(path)], cwd=local_clone)


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
) -> str | None:
    # Check if any files are staged before committing, if none, bail
    exit_code, _, _ = await run_subprocess(
        ["git", "diff", "--cached", "--quiet"],
        cwd=local_clone,
    )
    # 1 = staged, 0 = none staged
    if exit_code == 0:
        logger.info("No files staged for commit, halting.")
        raise RuntimeError("No files staged for commit, halting.")
    await check_subprocess(["git", "commit", "-m", commit_message], cwd=local_clone)
    if commit_only:
        return None
    await run_tool(
        "push_to_remote_repository",
        repository=fork_url,
        clone_path=str(local_clone),
        branch=update_branch,
        force=True,
        available_tools=available_tools,
    )
    return await run_tool(
        "open_merge_request",
        fork_url=fork_url,
        title=mr_title,
        description=mr_description,
        target=dist_git_branch,
        source=update_branch,
        available_tools=available_tools,
    )


async def comment_in_jira(
    jira_issue: str,
    agent_type: str,
    comment_text: str,
    available_tools: list[Tool],
) -> None:
    await run_tool(
        "add_jira_comment",
        issue_key=jira_issue,
        comment=JIRA_COMMENT_TEMPLATE.substitute(AGENT_TYPE=agent_type, JIRA_COMMENT=comment_text),
        private=True,
        available_tools=available_tools,
    )


async def change_jira_status(
    jira_issue: str,
    status: str,
    available_tools: list[Tool],
) -> None:
    await run_tool(
        "change_jira_status",
        issue_key=jira_issue,
        status=status,
        available_tools=available_tools,
    )


async def set_jira_labels(
    jira_issue: str,
    labels_to_add: list[str] | None = None,
    labels_to_remove: list[str] | None = None,
    dry_run: bool = False
) -> None:
    if dry_run:
        logger.info(f"Dry run, not updating labels for {jira_issue}")
        return

    try:
        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            await run_tool(
                "edit_jira_labels",
                issue_key=jira_issue,
                labels_to_add=labels_to_add or [],
                labels_to_remove=labels_to_remove or [],
                available_tools=gateway_tools,
            )

    except Exception as e:
        logger.warning(f"Failed to update labels for {jira_issue}: {e}")
