import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


REPO_CLEANUP_DAYS = 7

APPLICABILITY_DIR = "applicability"
MERGE_REQUESTS_DIR = "merge_requests"
NESTED_WORK_DIRS = {APPLICABILITY_DIR, MERGE_REQUESTS_DIR}


def _remove_if_stale(path: Path, cutoff_time: datetime) -> bool:
    """Delete *path* if its mtime predates *cutoff_time*. Return True on deletion."""
    mod_time = datetime.fromtimestamp(path.stat().st_mtime)
    if mod_time < cutoff_time:
        logger.info(f"Deleting old directory: {path}")
        shutil.rmtree(path, ignore_errors=False)
        return True
    return False


def cleanup_stale_directories(git_repos_path: Path, cutoff_time: datetime) -> int:
    """
    Finds and deletes stale directories in the specified path.
    Top-level directories are checked directly; known container directories
    (see NESTED_WORK_DIRS) are stepped into and their children are checked
    individually.
    Ignores all exceptions that could occur during cleanup.
    Return the number of deleted directories.
    """
    deleted_count = 0
    for item_path in git_repos_path.iterdir():
        try:
            if not item_path.is_dir():
                continue

            if item_path.name in NESTED_WORK_DIRS:
                for child in item_path.iterdir():
                    try:
                        if child.is_dir() and _remove_if_stale(child, cutoff_time):
                            deleted_count += 1
                    except Exception as ex:
                        logger.warning(f"Failed to delete directory {child}: {ex}")
                continue

            if _remove_if_stale(item_path, cutoff_time):
                deleted_count += 1
        except Exception as ex:
            logger.warning(f"Failed to process directory {item_path}: {ex}")
            continue

    return deleted_count


async def clean_stale_repositories() -> int:
    """
    Cleans up stale repositories (older than REPO_CLEANUP_DAYS days).

    Don't raise an error if the cleanup fails.
    Return the number of deleted directories.
    """
    git_repos_path_str = os.environ["GIT_REPO_BASEPATH"]

    logger.info(f"Cleaning directories in {git_repos_path_str} older than {REPO_CLEANUP_DAYS} days")

    git_repos_path = Path(git_repos_path_str)
    if not git_repos_path.is_dir():
        logger.info(f"Git repos path {git_repos_path_str} is not a directory. Skipping cleanup.")
        return 0

    cutoff_time = datetime.now() - timedelta(days=REPO_CLEANUP_DAYS)

    deleted_count = await asyncio.to_thread(cleanup_stale_directories, git_repos_path, cutoff_time)
    logger.info(f"Repository cleanup completed successfully. Deleted {deleted_count} directories.")
    return deleted_count
