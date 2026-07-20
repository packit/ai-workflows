import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


REPO_CLEANUP_DAYS = 7


_NESTED_WORK_DIRS = {"applicability", "merge_requests"}


def cleanup_stale_directories(git_repos_path: Path, cutoff_time: datetime) -> int:
    """
    Finds and deletes stale directories in the specified path.
    Top-level directories are checked directly; known container directories
    (applicability/, merge_requests/) are stepped into and their children
    are checked individually.
    Ignores all exceptions that could occur during cleanup.
    Return the number of deleted directories.
    """
    deleted_count = 0
    for item_path in git_repos_path.iterdir():
        try:
            if not item_path.is_dir():
                continue

            if item_path.name in _NESTED_WORK_DIRS:
                deleted_count += _cleanup_children(item_path, cutoff_time)
                continue

            mod_time = datetime.fromtimestamp(item_path.stat().st_mtime)
            if mod_time < cutoff_time:
                logger.info(f"Deleting old directory: {item_path}")
                shutil.rmtree(item_path, ignore_errors=False)
                deleted_count += 1
        except Exception as ex:
            logger.warning(f"Failed to delete directory {item_path}: {ex}")
            continue

    return deleted_count


def _cleanup_children(container: Path, cutoff_time: datetime) -> int:
    """Remove stale subdirectories inside a container directory."""
    deleted = 0
    for child in container.iterdir():
        try:
            if not child.is_dir():
                continue
            mod_time = datetime.fromtimestamp(child.stat().st_mtime)
            if mod_time < cutoff_time:
                logger.info(f"Deleting old directory: {child}")
                shutil.rmtree(child, ignore_errors=False)
                deleted += 1
        except Exception as ex:
            logger.warning(f"Failed to delete directory {child}: {ex}")
            continue
    return deleted


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
