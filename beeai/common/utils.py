"""
Common utility functions shared across the BeeAI system.
"""

import inspect
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Annotated, AsyncGenerator, Awaitable, TypeVar

import redis.asyncio as redis
from pydantic import AfterValidator

logger = logging.getLogger(__name__)


T = TypeVar("T")


def is_absolute(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("Argument must be an absolute path")
    return value


AbsolutePath = Annotated[Path, AfterValidator(is_absolute)]


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
