import asyncio
import errno
import logging
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


REPO_CLEANUP_DAYS = 7


def sanitize_url(text: str) -> str:
    """Remove oauth2:{token}@ credentials from URLs in error messages."""
    return re.sub(r"oauth2:[^@\s]+@", "oauth2:***@", text)


APPLICABILITY_DIR = "applicability"
MERGE_REQUESTS_DIR = "merge_requests"
EXECUTION_WORK_DIRS = {"Backport", "Rebase", "Rebuild"}
NESTED_WORK_DIRS = {APPLICABILITY_DIR, MERGE_REQUESTS_DIR, *EXECUTION_WORK_DIRS}
ACTIVE_WORKSPACE_MARKER = ".ymir-active"


def _is_safe_directory(path: Path, basepath: Path) -> bool:
    """Return whether *path* is a real directory strictly below *basepath*."""
    return not path.is_symlink() and path.is_dir() and path.resolve().is_relative_to(basepath)


def _is_active_workspace(path: Path, cutoff_time: datetime) -> bool:
    marker = path / ACTIVE_WORKSPACE_MARKER
    return (
        marker.is_file()
        and not marker.is_symlink()
        and datetime.fromtimestamp(marker.stat().st_mtime) >= cutoff_time
    )


def _remove_if_stale(path: Path, cutoff_time: datetime, basepath: Path) -> bool:
    """Delete *path* if its mtime predates *cutoff_time*. Return True on deletion."""
    if not _is_safe_directory(path, basepath):
        return False
    mod_time = datetime.fromtimestamp(path.stat().st_mtime)
    if mod_time < cutoff_time:
        logger.info(f"Deleting old directory: {path}")
        shutil.rmtree(path, ignore_errors=False)
        return True
    return False


def _remove_if_empty(path: Path) -> None:
    """Remove an empty directory, tolerating concurrent workspace changes."""
    try:
        path.rmdir()
    except FileNotFoundError:
        pass
    except OSError as error:
        if error.errno != errno.ENOTEMPTY:
            raise


def cleanup_stale_directories(git_repos_path: Path, cutoff_time: datetime) -> int:
    """
    Finds and deletes stale directories in the specified path.
    Top-level directories are checked directly; known container directories
    (see NESTED_WORK_DIRS) are stepped into and their children are checked
    individually.
    Ignores all exceptions that could occur during cleanup.
    Return the number of deleted directories.
    """
    basepath = git_repos_path.resolve()
    deleted_count = 0
    for item_path in git_repos_path.iterdir():
        try:
            if not _is_safe_directory(item_path, basepath):
                continue

            if item_path.name in NESTED_WORK_DIRS:
                for child in item_path.iterdir():
                    try:
                        if not _is_safe_directory(child, basepath):
                            continue
                        if item_path.name in EXECUTION_WORK_DIRS:
                            for workspace in child.iterdir():
                                try:
                                    if not _is_safe_directory(workspace, basepath):
                                        continue
                                    if _is_active_workspace(workspace, cutoff_time):
                                        continue
                                    if _remove_if_stale(workspace, cutoff_time, basepath):
                                        deleted_count += 1
                                except Exception as ex:
                                    logger.warning(f"Failed to delete directory {workspace}: {ex}")
                            _remove_if_empty(child)
                            continue
                        if _remove_if_stale(child, cutoff_time, basepath):
                            deleted_count += 1
                    except Exception as ex:
                        logger.warning(f"Failed to delete directory {child}: {ex}")
                continue

            if _remove_if_stale(item_path, cutoff_time, basepath):
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
