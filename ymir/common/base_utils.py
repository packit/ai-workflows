import asyncio
import base64
import contextlib
import inspect
import logging
import os
import re
import shlex
import signal
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


async def _race_shutdown(
    coro: Coroutine,
    shutdown_event: asyncio.Event,
    *,
    cancel_on_shutdown: bool,
) -> tuple[bool, object, asyncio.Task | None]:
    """Race `coro` against `shutdown_event`.

    Returns `(True, result, None)` if `coro` finished first.

    If shutdown fires first: returns `(False, None, None)` when
    `cancel_on_shutdown=True`, or `(False, None, work_task)` when
    `cancel_on_shutdown=False` — see below.

    `cancel_on_shutdown` controls what happens to `coro` when shutdown
    wins: pure in-process awaitables (`Semaphore.acquire`, `Event.wait`)
    are safe to cancel-and-await immediately. Live I/O (a Redis BRPOP or a
    custom `poll_fn`) is not: redis-py never disconnects/resets a
    connection on `CancelledError`, it just returns it to the pool as-is
    in a `finally`. Cancelling mid-read risks a stale response sitting on
    that connection, which the next command drawn from the pool (e.g. our
    own re-push RPUSH calls on shutdown) could read instead of its own.
    So for live I/O, `coro` is left running and handed back to the caller
    as `work_task` instead: if it eventually returns a real task (rather
    than timing out with `None`), the caller must still account for it
    (e.g. re-push it) rather than silently letting the result vanish.
    """
    work_task = asyncio.create_task(coro)
    stop_task = asyncio.create_task(shutdown_event.wait())
    try:
        done, _ = await asyncio.wait({work_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        work_task.cancel()
        stop_task.cancel()
        raise

    if stop_task in done and work_task not in done:
        if cancel_on_shutdown:
            work_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work_task
            return False, None, None
        return False, None, work_task

    with contextlib.suppress(asyncio.CancelledError):
        stop_task.cancel()
        await stop_task
    return True, work_task.result(), None


def install_shutdown_handler(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
) -> None:
    """Install SIGTERM/SIGINT handlers that set `shutdown_event`.

    Setting an Event (rather than cancelling a task directly) means the
    handler is safe no matter what the process is doing at signal time —
    it doesn't interrupt arbitrary awaits outside `run_task_loop`, it just
    asks the task loop to start winding down at its next checkpoint.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)


async def run_task_loop(
    redis_conn: redis.Redis,
    queues: list[str],
    process_fn: Callable[[bytes], Coroutine],
    max_concurrent: int = 1,
    poll_timeout: int = 30,
    poll_fn: Callable[[], Coroutine] | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run a concurrent task loop that pops tasks from Redis queues.

    Acquires a semaphore slot before popping to ensure we never hold more
    tasks in memory than we can process, preventing task loss on crash.

    On `shutdown_event`, stops pulling new tasks, cancels whatever is
    still in flight, and re-pushes their original payloads back to the
    queues they came from via RPUSH (tail — next to be popped by BRPOP)
    so no work is silently dropped. There's no grace period for that:
    task processing runs for minutes (sometimes hours, e.g. build
    polling), so waiting for in-flight work to finish naturally would
    rarely succeed and just delays recovery. Callers that can tolerate
    losing work outright can simply not pass `shutdown_event`.

    A poll (BRPOP or `poll_fn`) already in flight when shutdown fires is
    a different story: it's abandoned rather than cancelled (see
    `_race_shutdown`'s docstring), but it's bounded by `poll_timeout` and
    can still return a real task once it resolves. That result is waited
    for and re-pushed too, so a task isn't silently consumed from Redis
    and dropped just because it arrived a moment after shutdown.
    """
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be at least 1")

    shutdown_event = shutdown_event or asyncio.Event()
    sem = asyncio.Semaphore(max_concurrent)
    active: dict[asyncio.Task, tuple[bytes, bytes]] = {}

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

    async def _default_poll() -> tuple[bytes, bytes] | None:
        # Already (queue_name, payload) or None; both come back as raw
        # bytes since the client doesn't set decode_responses.
        return await fix_await(redis_conn.brpop(queues, timeout=poll_timeout))

    actual_poll = poll_fn or _default_poll
    orphan_poll_task: asyncio.Task | None = None

    while not shutdown_event.is_set():
        # Race the semaphore acquire against shutdown too — not just the
        # poll. Without this, once all `max_concurrent` slots are held by
        # genuinely long-running tasks (the exact scenario this exists
        # for — e.g. build-polling that can run for hours), `sem.acquire()`
        # would block forever waiting for a slot that never frees up
        # naturally, and the loop would never reach the shutdown check at
        # all: SIGTERM would fire and nothing would happen.
        acquired, _, _ = await _race_shutdown(sem.acquire(), shutdown_event, cancel_on_shutdown=True)
        if not acquired:
            break
        if shutdown_event.is_set():
            sem.release()
            break

        # Race the poll against shutdown so we don't block on BRPOP (or a
        # custom poll_fn) after shutdown has been requested. Live I/O, so
        # cancel_on_shutdown=False — see _race_shutdown's docstring for
        # why cancelling a live Redis call mid-flight is unsafe. If
        # shutdown wins, the poll is still running; hang onto it so it
        # can be resolved (and re-pushed if it returns a task) below.
        got_result, result, orphan_poll_task = await _race_shutdown(
            actual_poll(), shutdown_event, cancel_on_shutdown=False
        )
        if not got_result:
            sem.release()
            break

        if result is None:
            sem.release()
            if poll_fn:
                # Race the idle-sleep against shutdown too, so a custom
                # poll_fn doesn't add up to poll_timeout of extra
                # shutdown latency on top of everything else.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown_event.wait(), timeout=poll_timeout)
            continue

        task_loop_logger.info("Received task from queue.")

        source_queue, payload = result
        t = asyncio.create_task(_run(payload))
        active[t] = (source_queue, payload)
        t.add_done_callback(lambda _t: active.pop(_t, None))

    if active:
        # Snapshot BEFORE cancelling: the done-callback above pops entries
        # out of `active` as each task transitions to done, which happens
        # *during* the gather below. Iterating `active` after the gather
        # would see an already-emptied dict and silently re-push nothing.
        # Filter out tasks that are already done() — done-callbacks run via
        # call_soon, so a completed task can still be in `active` briefly;
        # re-pushing it would duplicate work that already ran.
        to_repush = [(t, meta) for t, meta in active.items() if not t.done()]
        task_loop_logger.info(
            "Shutting down: cancelling %d active task(s) and re-pushing to Redis",
            len(to_repush),
        )
        for t, _ in to_repush:
            t.cancel()
        await asyncio.gather(*(t for t, _ in to_repush), return_exceptions=True)

        for _t, (source_queue, payload) in to_repush:
            try:
                await fix_await(redis_conn.rpush(source_queue, payload))
                task_loop_logger.info("Re-pushed task to %s on shutdown", source_queue)
            except Exception:
                task_loop_logger.exception("Failed to re-push task to %s on shutdown", source_queue)

    if orphan_poll_task is not None:
        # Bounded by poll_timeout (BRPOP's own timeout, or a well-behaved
        # poll_fn) — not an indefinite wait, so this doesn't reintroduce a
        # grace period for task processing. Must be resolved here, before
        # returning, rather than left for the event loop to clean up on
        # its own: otherwise a task it pops from Redis after we've already
        # returned is consumed and dropped with nothing to re-push it.
        task_loop_logger.info(
            "Shutting down: waiting on in-flight poll (bounded by poll_timeout=%ds)",
            poll_timeout,
        )
        try:
            orphan_result = await orphan_poll_task
            if orphan_result is not None:
                source_queue, payload = orphan_result
                await fix_await(redis_conn.rpush(source_queue, payload))
                task_loop_logger.info("Re-pushed orphaned poll result to %s on shutdown", source_queue)
        except Exception:
            logger.exception("Failed to resolve or re-push orphaned poll task after shutdown")


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
