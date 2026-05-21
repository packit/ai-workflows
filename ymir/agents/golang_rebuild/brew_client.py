"""
Brew Build Client (async)

Handles Brew build operations:
- Trigger scratch builds
- Trigger final builds
- Monitor build status
- Get build information

Uses brew command-line tool (requires Kerberos authentication).
"""

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

from ymir.agents.golang_rebuild.constants import BREW_URL
from ymir.agents.golang_rebuild.models import BuildResult

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


class BrewClient:
    """Client for Brew build system operations (async)."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.brew_url = self.config.get("brew", {}).get("url", BREW_URL)
        self.poll_interval = self.config.get("brew", {}).get("poll_interval", 30)
        self.max_wait_time = self.config.get("brew", {}).get("max_wait_time", 7200)

    async def _run_brew_command(self, command: list[str], check: bool = True) -> tuple[int, str, str]:
        """Run brew command."""
        return await _run_command(["brew", *command], check=check)

    async def scratch_build(
        self,
        repo_path: Path,
        target: str,
        release: str | None = None,
    ) -> str:
        """
        Trigger scratch build using rhpkg. Returns task ID.

        Args:
            release: Optional --release flag for side-tag builds
                     (e.g., "rhel-9.4.0" for side-tag target)
        """
        logger.info(f"Triggering scratch build for {repo_path} (target: {target}, release: {release})")
        cmd = ["rhpkg"]
        if release:
            cmd.append(f"--release={release}")
        cmd.extend(["scratch-build", "--srpm", f"--target={target}"])
        _, stdout, _ = await _run_command(cmd, cwd=repo_path)
        task_id = self._extract_task_id(stdout)
        if not task_id:
            raise ValueError(f"Failed to extract task ID from scratch build output:\n{stdout}")
        logger.info(f"Scratch build task created: {task_id}")
        return task_id

    async def final_build(
        self,
        repo_path: Path,
        target: str,
        release: str | None = None,
    ) -> str:
        """
        Trigger final build using rhpkg. Returns task ID.

        Args:
            release: Optional --release flag for side-tag builds
        """
        logger.info(f"Triggering final build for {repo_path} (target: {target}, release: {release})")
        cmd = ["rhpkg"]
        if release:
            cmd.append(f"--release={release}")
        cmd.extend(["build", f"--target={target}"])
        _, stdout, _ = await _run_command(
            cmd,
            cwd=repo_path,
        )
        task_id = self._extract_task_id(stdout)
        if not task_id:
            raise ValueError(f"Failed to extract task ID from build output:\n{stdout}")
        logger.info(f"Final build task created: {task_id}")
        return task_id

    def _extract_task_id(self, output: str) -> str | None:
        """Extract task ID from rhpkg/brew output."""
        for pattern in [r"Created task:\s*(\d+)", r"Task ID:\s*(\d+)", r"taskID=(\d+)"]:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    async def get_task_info(self, task_id: str) -> dict:
        """Get task information."""
        _, stdout, _ = await self._run_brew_command(["taskinfo", task_id])
        info = {}
        for line in stdout.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                info[key.strip()] = value.strip()
        return info

    async def get_task_state(self, task_id: str) -> tuple[str | None, str | None]:
        """Get task state and result."""
        info = await self.get_task_info(task_id)
        state = info.get("State", "").lower()
        result = None
        if state == "closed":
            result_text = info.get("Result", "").lower()
            result = "success" if ("success" in result_text or result_text == "0") else "fail"
        return state, result

    async def is_task_finished(self, task_id: str) -> tuple[bool, str | None]:
        """Check if task is finished."""
        state, result = await self.get_task_state(task_id)
        if state == "closed":
            return True, result
        if state in ["canceled", "failed"]:
            return True, "fail"
        return False, None

    async def wait_for_task(
        self,
        task_id: str,
        poll_interval: int | None = None,
        max_wait: int | None = None,
        callback: Callable | None = None,
    ) -> BuildResult:
        """Wait for task to complete, polling periodically."""
        poll_interval = poll_interval or self.poll_interval
        max_wait = max_wait or self.max_wait_time

        logger.info(f"Waiting for task {task_id} (poll every {poll_interval}s, max {max_wait}s)")
        elapsed = 0

        while True:
            is_finished, result = await self.is_task_finished(task_id)

            if callback:
                state, _ = await self.get_task_state(task_id)
                callback(task_id, state, elapsed)

            if is_finished:
                logger.info(f"Task {task_id} finished with result: {result}")
                build_info = await self.get_build_info_from_task(task_id)
                return BuildResult(
                    task_id=task_id,
                    nvr=build_info.get("nvr"),
                    state=result,
                    success=(result == "success"),
                    build_url=f"{self.brew_url}/taskinfo?taskID={task_id}",
                )

            if elapsed >= max_wait:
                logger.error(f"Task {task_id} timed out after {elapsed}s")
                return BuildResult(
                    task_id=task_id,
                    state="timeout",
                    success=False,
                    error_message=f"Build timed out after {elapsed}s",
                    build_url=f"{self.brew_url}/taskinfo?taskID={task_id}",
                )

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    async def get_build_info_from_task(self, task_id: str) -> dict:
        """Get build information from task."""
        info = await self.get_task_info(task_id)
        nvr = None
        if "Build" in info:
            match = re.match(r"([^\s]+)\s*\((\d+)\)", info["Build"])
            if match:
                nvr = match.group(1)
        return {"task_id": task_id, "nvr": nvr, "info": info}

    async def get_latest_build(self, tag: str, package: str) -> str | None:
        """Get latest build NVR for a package in a tag."""
        returncode, stdout, _ = await self._run_brew_command(["latest-build", tag, package], check=False)
        if returncode != 0:
            return None
        lines = stdout.strip().splitlines()
        if len(lines) >= 3:
            return lines[2].split()[0]
        return None

    async def build_and_wait(
        self,
        repo_path: Path,
        target: str,
        scratch: bool = False,
        release: str | None = None,
        callback: Callable | None = None,
    ) -> BuildResult:
        """Trigger build and wait for completion."""
        if scratch:
            task_id = await self.scratch_build(repo_path, target, release=release)
        else:
            task_id = await self.final_build(repo_path, target, release=release)
        return await self.wait_for_task(task_id, callback=callback)

    async def verify_kerberos_auth(self) -> bool:
        """Verify Kerberos authentication is valid."""
        try:
            returncode, _, _ = await _run_command(["klist", "-s"], check=False)
            return returncode == 0
        except FileNotFoundError:
            logger.error("klist command not found - Kerberos tools not installed")
            return False
