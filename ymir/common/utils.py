"""
Common utility functions shared across the BeeAI system.
"""

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import shlex
import subprocess
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import redis.asyncio as redis
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.mcp import MCPTool
from beeai_framework.tools.types import JSONToolOutput, StringToolOutput
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def fix_await(v: T | Awaitable[T]) -> T:
    """
    Work around typing problems in the asyncio redis client.

    Typing for the asyncio redis client is messed up, and functions
    return `T | Awaitable[T]` instead of `T`. This function
    fixes the type error by asserting that the value is awaitable
    before awaiting it.

    For a proper fix, see: https://github.com/redis/redis-py/pull/3619


    Usage: `await fixAwait(redis.get("key"))`
    """
    assert inspect.isawaitable(v)
    return await v


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
    client = redis.Redis.from_url(redis_url)
    try:
        await client.ping()
        logger.debug("Connected to Redis")
        yield client
    finally:
        await client.aclose()
        logger.debug("Disconnected from Redis")


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
        print(stdout.decode(), flush=True)
        print(stderr.decode(), flush=True)
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

    # klist exits with a status of 1 if no cache file exists, so we
    # need to check for the file first.

    ccache_file = os.getenv("KRB5CCNAME")
    if not ccache_file:
        raise KerberosError("KRB5CCNAME environment variable is not set")

    # Parse KRB5CCNAME which can be in the format TYPE:value (e.g., FILE:/path, KCM:1000)
    # Only check file existence if TYPE is FILE and the file doesn't exist
    should_run_klist = True
    if ":" in ccache_file:
        cache_type, cache_path = ccache_file.split(":", 1)
        if cache_type == "FILE" and not os.path.exists(cache_path):
            should_run_klist = False
    else:
        # Legacy format without type prefix - treat as a file path
        if not os.path.exists(ccache_file):
            should_run_klist = False

    if should_run_klist:
        proc = await asyncio.create_subprocess_exec(
            "klist",
            "-l",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        # klist returns an exit status of 1 if
        if proc.returncode:
            print(stdout.decode(), flush=True)
            print(stderr.decode(), flush=True)
            raise KerberosError("Failed to list Kerberos tickets")

        principals = [
            parts[0]
            for line in stdout.decode().splitlines()
            if "Expired" not in line
            for parts in (line.split(),)
            if len(parts) >= 1 and "@" in parts[0]
        ]
    else:
        principals = []

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
            print(stdout.decode(), flush=True)
            print(stderr.decode(), flush=True)
            raise KerberosError("kinit command failed")
        logger.info("Initialized Kerberos ticket for %s", keytab_principal)
        return keytab_principal

    if principals:
        logger.info("Using existing ticket for %s", principals[0])
        return principals[0]
    raise KerberosError("No valid Kerberos ticket found and KEYTAB_FILE is not set")


def get_absolute_path(path: Path, tool: Tool) -> Path:
    if path.is_absolute():
        return path
    cwd = (tool.options or {}).get("working_directory") or Path.cwd()
    return Path(cwd) / path


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


async def run_tool(
    tool: str | Tool,
    available_tools: list[Tool] | None = None,
    **kwargs: Any,
) -> str | dict:
    if isinstance(tool, str):
        tool = next(t for t in available_tools or [] if t.name == tool)
    output = await tool.run(input=kwargs).middleware(GlobalTrajectoryMiddleware(pretty=True))
    match output:
        case StringToolOutput():
            result = output.get_text_content()
        case JSONToolOutput():
            result = output.to_json_safe()
        case _:
            result = str(output)
    if isinstance(result, list):
        [result] = result
    if isinstance(result, TextContent):
        result = result.text
    if isinstance(result, dict) and len(result) == 1 and "result" in result:
        result = result["result"]

    # loads twice here is neccessary, because beeai mcp server unfortunately wraps the
    # JSON object twice
    # this has been fixed in BeeAI 0.1.58
    # FIXME: Once BeeAI is updated remove this workaround
    try:
        result = json.loads(result)
        result = json.loads(result)
    except json.JSONDecodeError:
        pass

    return result


@asynccontextmanager
async def mcp_tools(
    sse_url: str, filter: Callable[[str], bool] | None = None
) -> AsyncGenerator[list[MCPTool]]:
    async with (
        sse_client(sse_url) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await MCPTool.from_client(session)
        if filter:
            tools = [t for t in tools if filter(t.name)]
        yield tools
