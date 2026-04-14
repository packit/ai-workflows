import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


REPO_CLEANUP_DAYS = 14


def cleanup_stale_directories(git_repos_path: Path, cutoff_time: datetime) -> int:
    """
    Finds and deletes stale directories in the specified path.
    Ignores all exceptions that could occur during cleanup.
    Return the number of deleted directories.
    """
    deleted_count = 0
    for item_path in git_repos_path.iterdir():
        try:
            if not item_path.is_dir() or not item_path.name.lower().startswith("rhel-"):
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

async def clean_stale_repositories() -> int:
    """
    Cleans up stale repositories (older than 14 days).

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
