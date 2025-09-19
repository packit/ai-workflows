"""
Common utility functions shared across the BeeAI system.
"""

import inspect
import logging
import re
from contextlib import asynccontextmanager
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
