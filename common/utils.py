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

    if os.path.exists(ccache_file):
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
    else:
        raise KerberosError("No valid Kerberos ticket found and KEYTAB_FILE is not set")
