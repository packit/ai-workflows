import itertools
import os
import re
import shutil
from pathlib import Path
from typing import Tuple

from beeai_framework.tools import Tool

from constants import BRANCH_PREFIX, JIRA_COMMENT_TEMPLATE
from utils import check_subprocess, run_tool


async def fork_and_prepare_dist_git(
    jira_issue: str,
    package: str,
    dist_git_branch: str,
    available_tools: list[Tool],
) -> Tuple[Path, str, str]:
    working_dir = Path(os.environ["GIT_REPO_BASEPATH"]) / jira_issue
    working_dir.mkdir(parents=True, exist_ok=True)
    namespace = "centos-stream" if re.match(r"^c\d+s$", dist_git_branch) else "rhel"
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
    return local_clone, update_branch, fork_url


async def commit_push_and_open_mr(
    local_clone: Path,
    files_to_commit: str | list[str],
    commit_message: str,
    fork_url: str,
    dist_git_branch: str,
    update_branch: str,
    mr_title: str,
    mr_description: str,
    available_tools: list[Tool],
    commit_only: bool = False,
) -> str | None:
    if isinstance(files_to_commit, str):
        files_to_commit = [files_to_commit]
    for path in itertools.chain(*(local_clone.glob(pat) for pat in files_to_commit)):
        await check_subprocess(["git", "add", str(path)], cwd=local_clone)
    # TODO: check for empty commit (the command below will fail anyway, but we need to handle this somehow)
    await check_subprocess(["git", "commit", "-m", commit_message], cwd=local_clone)
    if commit_only:
        return None
    await run_tool(
        "push_to_remote_repository",
        repository=fork_url,
        clone_path=local_clone,
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
