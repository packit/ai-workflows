import asyncio
import base64
import inspect
import logging
import os
import re
import shlex
import subprocess
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar

import redis.asyncio as redis

from ymir.common.logging_setup import current_jira_issue, flush_task_logs

logger = logging.getLogger(__name__)
task_loop_logger = logging.getLogger("agent.task_loop")

T = TypeVar("T")


async def fix_await(v: T | Awaitable[T]) -> T:
    """
    Work around typing problems in the asyncio redis client.

    Typing for the asyncio redis client is messed up, and functions
    return `T | Awaitable[T]` instead of `T`. This function
    fixes the type error by either awaiting, if the value is awaitable
    or returning it immediately.

    For a proper fix, see: https://github.com/redis/redis-py/pull/3619

    Usage: `await fix_await(redis.get("key"))`
    """
    if inspect.isawaitable(v):
        return await v
    return v


@asynccontextmanager
async def redis_client(redis_url: str) -> AsyncGenerator[redis.Redis]:
    """
    Create a Redis client with proper connection management.

    Args:
        redis_url: Redis connection URL (e.g., redis://localhost:6379/0)

    Yields:
        redis.Redis: Connected Redis client

    Example:
        async with redis_client("redis://localhost:6379/0") as client:
            await client.ping()
    """
    client = redis.Redis.from_url(redis_url, socket_timeout=None)
    try:
        await client.ping()
        logger.debug("Connected to Redis")
        yield client
    finally:
        await client.aclose()
        logger.debug("Disconnected from Redis")


async def run_task_loop(
    redis_conn: redis.Redis,
    queues: list[str],
    process_fn: Callable[[bytes], Coroutine],
    max_concurrent: int = 1,
    poll_timeout: int = 30,
    poll_fn: Callable[[], Coroutine] | None = None,
) -> None:
    """Run a concurrent task loop that pops tasks from Redis queues.

    Acquires a semaphore slot before popping to ensure we never hold more
    tasks in memory than we can process, preventing task loss on crash.
    """
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be at least 1")

    sem = asyncio.Semaphore(max_concurrent)
    active: set[asyncio.Task] = set()

    label = "custom poller" if poll_fn else str(queues)
    task_loop_logger.info(
        "Task loop started: listening on %s, max_concurrent=%d",
        label,
        max_concurrent,
    )

    async def _run(payload: bytes) -> None:
        try:
            await process_fn(payload)
        except Exception:
            logger.exception("Unhandled exception in task processing")
        finally:
            try:
                if issue := current_jira_issue.get():
                    current_jira_issue.set(None)
                    flush_task_logs(issue)
            except Exception:
                logger.exception("Unhandled exception during log flushing")
            sem.release()

    async def _default_poll() -> bytes | None:
        element = await fix_await(redis_conn.brpop(queues, timeout=poll_timeout))
        if element is None:
            return None
        _, payload = element
        return payload

    actual_poll = poll_fn or _default_poll

    try:
        while True:
            await sem.acquire()
            payload = await actual_poll()
            if payload is None:
                sem.release()
                if poll_fn:
                    await asyncio.sleep(poll_timeout)
                continue

            task_loop_logger.info("Received task from queue.")

            t = asyncio.create_task(_run(payload))
            active.add(t)
            t.add_done_callback(active.discard)
    except asyncio.CancelledError:
        if active:
            task_loop_logger.info(
                "Task loop cancelled. Waiting for %d active tasks to complete...",
                len(active),
            )
            await asyncio.shield(asyncio.gather(*active, return_exceptions=True))
        raise
    finally:
        if active:
            task_loop_logger.info("Cancelling %d active tasks...", len(active))
            for t in active:
                t.cancel()
            await asyncio.gather(*active, return_exceptions=True)


def get_jira_auth_headers() -> dict[str, str]:
    """Build Jira API authentication headers.

    Uses Basic Auth (email + API token) for Atlassian Cloud.
    Reads JIRA_EMAIL and JIRA_TOKEN from environment variables.
    """
    jira_email = os.environ["JIRA_EMAIL"]
    jira_token = os.environ["JIRA_TOKEN"]
    credentials = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


CS_BRANCH_PATTERN = re.compile(r"^c\d+s$")


def is_cs_branch(dist_git_branch: str) -> bool:
    return CS_BRANCH_PATTERN.match(dist_git_branch) is not None


class KerberosError(Exception):
    pass


def parse_klist_principals(output: str) -> list[str]:
    """Return non-expired principals from the text output of `klist -l`."""
    return [
        parts[0]
        for line in output.splitlines()
        if "Expired" not in line
        for parts in (line.split(),)
        if len(parts) >= 1 and "@" in parts[0]
    ]


async def extract_principal(keytab_file: str) -> str:
    """
    Extracts principal from the specified keytab file. Assumes that there is
    a single principal in the keytab.

    Args:
        keytab_file: Path to a keytab file.

    Returns:
        Extracted principal.
    """
    proc = await asyncio.create_subprocess_exec(
        "klist",
        "-k",
        "-K",
        "-e",
        keytab_file,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode:
        logger.error("klist command failed:\nstdout: %s\nstderr: %s", stdout.decode(), stderr.decode())
        raise KerberosError("klist command failed")
    key_pattern = re.compile(r"^\s*(\d+)\s+(\S+)\s+\((\S+)\)\s+\((\S+)\)$")
    for line in stdout.decode().splitlines():
        if not (match := key_pattern.match(line)):
            continue
        # just return the principal associated with the first key
        return match.group(2)
    raise KerberosError("No valid key found in the keytab file")


async def init_kerberos_ticket() -> str:
    """
    Initializes Kerberos ticket unless it's already present in a credentials cache.
    On success, returns the associated principal. Raises an exception if a ticket
    cannot be initialized or found.
    """
    keytab_principal = None
    keytab_file = os.getenv("KEYTAB_FILE")
    if keytab_file is not None:
        keytab_principal = await extract_principal(keytab_file)
        if not keytab_principal:
            raise KerberosError("Failed to extract principal from keytab file")

    # klist exits with a status of 1 if no credentials cache is found
    proc = await asyncio.create_subprocess_exec(
        "klist",
        "-l",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    klist_error = None
    if proc.returncode:
        klist_error = (
            f"klist exited with {proc.returncode}:\n"
            f"stdout: {stdout.decode(errors='replace')}\n"
            f"stderr: {stderr.decode(errors='replace')}"
        )
        principals = []
    else:
        principals = parse_klist_principals(stdout.decode())

    if keytab_file and keytab_principal:
        if keytab_principal in principals:
            logger.info("Using existing ticket for keytab principal %s", keytab_principal)
            return keytab_principal

        env = os.environ.copy()
        env.update({"KRB5_TRACE": "/dev/stdout"})
        proc = await asyncio.create_subprocess_exec(
            "kinit",
            "-k",
            "-t",
            keytab_file,
            keytab_principal,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode:
            logger.error("kinit command failed:\nstdout: %s\nstderr: %s", stdout.decode(), stderr.decode())
            raise KerberosError("kinit command failed")
        logger.info("Initialized Kerberos ticket for %s", keytab_principal)
        return keytab_principal

    if principals:
        logger.info("Using existing ticket for %s", principals[0])
        return principals[0]
    msg = "No valid Kerberos ticket found and KEYTAB_FILE is not set"
    if klist_error:
        msg += f"\n{klist_error}"
    raise KerberosError(msg)


async def run_subprocess(
    cmd: str | list[str],
    shell: bool = False,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str | None, str | None]:
    """Run a subprocess and return the exit code, stdout, and stderr."""
    kwargs = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = os.environ.copy()
        kwargs["env"].update(env)
    if shell:
        if not isinstance(cmd, str):
            cmd = shlex.join(cmd)
        proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
    else:
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        proc = await asyncio.create_subprocess_exec(cmd[0], *cmd[1:], **kwargs)
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode,
        stdout.decode() if stdout else None,
        stderr.decode() if stderr else None,
    )


async def check_subprocess(
    cmd: str | list[str],
    shell: bool = False,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    exit_code, stdout, stderr = await run_subprocess(cmd, shell, cwd, env)
    if exit_code:
        logger.error(
            "Command %s failed with exit code %d\nstdout: %s\nstderr: %s",
            cmd,
            exit_code,
            stdout,
            stderr,
        )
        raise subprocess.CalledProcessError(exit_code, cmd, stdout, stderr)
    return stdout, stderr
