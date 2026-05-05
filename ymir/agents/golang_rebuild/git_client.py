"""
Git Client for RHEL 8/9 Brew workflow (async).

Uses rhpkg for dist-git repositories. For RHEL 10+ GitLab workflow,
the agent uses agents/tasks.py:fork_and_prepare_dist_git() instead.
"""

import asyncio
import logging
from pathlib import Path

from ymir.agents.golang_rebuild.constants import COMMIT_MESSAGE_TEMPLATE
from ymir.agents.golang_rebuild.utils import format_cve_list, format_jira_list

logger = logging.getLogger(__name__)


async def _run_command(
    command: list[str],
    cwd: Path | None = None,
    check: bool = True,
) -> tuple[int, str, str]:
    """Run async subprocess command."""
    logger.debug(f"Running: {' '.join(command)}")
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stdout_str = stdout.decode() if stdout else ""
    stderr_str = stderr.decode() if stderr else ""

    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(command)}\n{stderr_str}")

    return proc.returncode, stdout_str, stderr_str


class GitClient:
    """Client for Git/rhpkg operations on RHEL dist-git repositories (async)."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.git_tool = self.config.get("git", {}).get("default_tool", "rhpkg")

    async def clone_repository(self, component: str, target_dir: Path, branch: str | None = None) -> Path:
        """Clone dist-git repository using rhpkg."""
        target_dir.mkdir(parents=True, exist_ok=True)
        repo_path = target_dir / component

        if repo_path.exists():
            logger.info(f"Repository already exists at {repo_path}, pulling updates")
            await self.pull(repo_path)
            if branch:
                await self.checkout_branch(repo_path, branch)
            return repo_path

        logger.info(f"Cloning {component} into {target_dir}")

        if self.git_tool == "rhpkg":
            await _run_command(["rhpkg", "clone", component], cwd=target_dir)
        elif self.git_tool == "fedpkg":
            await _run_command(["fedpkg", "clone", component], cwd=target_dir)
        else:
            repo_url = self.config.get("components", {}).get(component, {}).get("repo_url")
            if not repo_url:
                raise ValueError(f"No repository URL configured for {component}")
            await _run_command(["git", "clone", repo_url], cwd=target_dir)

        if branch:
            await self.checkout_branch(repo_path, branch)

        logger.info(f"Successfully cloned {component} to {repo_path}")
        return repo_path

    async def checkout_branch(self, repo_path: Path, branch: str):
        """Checkout a specific branch."""
        logger.info(f"Checking out branch: {branch}")
        returncode, _, _ = await _run_command(["git", "checkout", branch], cwd=repo_path, check=False)
        if returncode == 0:
            return

        await _run_command(["git", "fetch", "--all"], cwd=repo_path)
        for remote in ["origin", "rhel-gitlab", "centos-gitlab"]:
            returncode, _, _ = await _run_command(
                ["git", "checkout", "-b", branch, f"{remote}/{branch}"],
                cwd=repo_path,
                check=False,
            )
            if returncode == 0:
                return

        raise ValueError(f"Branch {branch} not found in any remote (origin, rhel-gitlab, centos-gitlab)")

    async def pull(self, repo_path: Path, branch: str | None = None):
        """Pull latest changes from remote."""
        if branch:
            await _run_command(["git", "pull", "origin", branch], cwd=repo_path)
        else:
            await _run_command(["git", "pull"], cwd=repo_path)

    async def get_current_branch(self, repo_path: Path) -> str:
        """Get current branch name."""
        _, stdout, _ = await _run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        return stdout.strip()

    async def has_staged_changes(self, repo_path: Path) -> bool:
        """Check if there are staged changes."""
        returncode, _, _ = await _run_command(
            ["git", "diff", "--cached", "--quiet"], cwd=repo_path, check=False
        )
        return returncode == 1

    async def has_uncommitted_changes(self, repo_path: Path) -> bool:
        """Check if repository has uncommitted changes."""
        _, stdout, _ = await _run_command(["git", "status", "--short"], cwd=repo_path)
        return bool(stdout.strip())

    async def stage_file(self, repo_path: Path, file_path: str | Path):
        """Stage a file for commit."""
        await _run_command(["git", "add", str(file_path)], cwd=repo_path)

    async def commit(
        self, repo_path: Path, message: str, author_name: str | None = None, author_email: str | None = None
    ):
        """Create a commit."""
        if not await self.has_staged_changes(repo_path):
            logger.warning("No staged changes to commit")
            return
        cmd = ["git", "commit", "-m", message]
        if author_name and author_email:
            cmd.extend(["--author", f"{author_name} <{author_email}>"])
        await _run_command(cmd, cwd=repo_path)

    async def commit_golang_rebuild(
        self,
        repo_path: Path,
        golang_version: str,
        cves: list[str],
        jiras: list[str],
        author_name: str,
        author_email: str,
    ):
        """Create commit for golang rebuild with standard message format."""
        message = COMMIT_MESSAGE_TEMPLATE.format(
            golang_version=golang_version,
            cves=format_cve_list(cves),
            jiras=format_jira_list(jiras),
            name=author_name,
            email=author_email,
        )
        await self.commit(repo_path, message, author_name, author_email)

    async def push(self, repo_path: Path, branch: str | None = None, force: bool = False):
        """Push commits to remote."""
        cmd = ["git", "push"]
        if force:
            cmd.append("--force")
        if branch:
            cmd.extend(["origin", branch])
        logger.info(f"Pushing to remote: {branch or 'current branch'}")
        await _run_command(cmd, cwd=repo_path)

    async def prepare_rebuild_commit(
        self,
        repo_path: Path,
        spec_file: str | Path,
        golang_version: str,
        cves: list[str],
        jiras: list[str],
        author_name: str,
        author_email: str,
    ) -> bool:
        """Stage spec file, commit with standard message. Returns True if committed."""
        await self.stage_file(repo_path, spec_file)
        if not await self.has_staged_changes(repo_path):
            logger.warning("No changes to commit in spec file")
            return False
        await self.commit_golang_rebuild(repo_path, golang_version, cves, jiras, author_name, author_email)
        return True

    async def verify_clean_state(self, repo_path: Path) -> tuple[bool, str]:
        """Verify repository is in clean state."""
        if await self.has_uncommitted_changes(repo_path):
            _, stdout, _ = await _run_command(["git", "status", "--short"], cwd=repo_path)
            return False, f"Repository has uncommitted changes:\n{stdout}"
        return True, "Repository is clean"

    async def verify_branch(self, repo_path: Path, expected_branch: str) -> tuple[bool, str]:
        """Verify repository is on expected branch."""
        current = await self.get_current_branch(repo_path)
        if current != expected_branch:
            return False, f"On branch '{current}', expected '{expected_branch}'"
        return True, f"On correct branch: {expected_branch}"

    async def download_sources(self, repo_path: Path, spec_file: str) -> str:
        """
        Download upstream sources using spectool.

        Args:
            repo_path: Path to repository
            spec_file: Spec file name (e.g., "buildah.spec")

        Returns:
            stdout from spectool
        """
        logger.info(f"Downloading sources: spectool -g {spec_file}")
        _, stdout, _ = await _run_command(
            ["spectool", "-g", spec_file],
            cwd=repo_path,
        )
        return stdout

    async def upload_new_sources(self, repo_path: Path) -> str:
        """
        Upload new sources to lookaside cache using rhpkg new-sources.

        Finds downloaded tarballs in the repo directory and uploads them.

        Returns:
            stdout from rhpkg new-sources
        """
        # Find tarballs (common source archive patterns)
        tarball_patterns = ["*.tar.gz", "*.tar.bz2", "*.tar.xz", "*.tgz", "*.zip"]
        tarballs = []
        for pattern in tarball_patterns:
            tarballs.extend(repo_path.glob(pattern))

        if not tarballs:
            raise FileNotFoundError(f"No source tarballs found in {repo_path}")

        tarball_names = [t.name for t in tarballs]
        logger.info(f"Uploading new sources: rhpkg new-sources {' '.join(tarball_names)}")

        _, stdout, _ = await _run_command(
            ["rhpkg", "new-sources", *tarball_names],
            cwd=repo_path,
        )
        return stdout

    async def update_sources_for_commit(self, repo_path: Path, spec_file: str) -> None:
        """
        Full source update flow: spectool -g -> rhpkg new-sources.

        Used when a new commit hash is set in the spec file and sources
        need to be re-downloaded and uploaded to lookaside cache.
        """
        logger.info("Updating sources for new commit hash")
        await self.download_sources(repo_path, spec_file)
        await self.upload_new_sources(repo_path)
