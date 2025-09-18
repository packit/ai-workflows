"""
Common utility functions shared across the BeeAI system.
"""

import asyncio
import inspect
import logging
import os
import re
from contextlib import asynccontextmanager
import subprocess
from typing import AsyncGenerator, Awaitable, TypeVar

import redis.asyncio as redis

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
async def redis_client(redis_url: str) -> AsyncGenerator[redis.Redis, None]:
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


CS_BRANCH_PATTERN = re.compile(r"^c\d+s$")


def is_cs_branch(dist_git_branch: str) -> bool:
    return CS_BRANCH_PATTERN.match(dist_git_branch) is not None


async def extract_principal(keytab_file: str) -> str | None:
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
    print(stdout.decode(), flush=True)
    if proc.returncode:
        print(stderr.decode(), flush=True)
        return None
    key_pattern = re.compile(r"^\s*(\d+)\s+(\S+)\s+\((\S+)\)\s+\((\S+)\)$")
    for line in stdout.decode().splitlines():
        if not (match := key_pattern.match(line)):
            continue
        # just return the principal associated with the first key
        return match.group(2)
    return None


async def init_kerberos_ticket() -> str | None:
    """
    Initializes Kerberos ticket unless it's already present in a credentials cache.
    On success, returns the associated principal.
    """
    keytab_file = os.getenv("KEYTAB_FILE")
    principal = await extract_principal(keytab_file)
    if not principal:
        print("Failed to extract principal", flush=True)
        return None
    proc = await asyncio.create_subprocess_exec(
        "klist",
        "-l",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    print(stdout.decode(), flush=True)
    if proc.returncode:
        print(stderr.decode(), flush=True)
    elif any(
        l for l in stdout.decode().splitlines() if principal in l and "Expired" not in l
    ):
        return principal
    env = os.environ.copy()
    env.update({"KRB5_TRACE": "/dev/stdout"})
    proc = await asyncio.create_subprocess_exec(
        "kinit", "-k", "-t", keytab_file, principal, env=env
    )
    return None if await proc.wait() else principal
