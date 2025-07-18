from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable

import redis.asyncio as redis
from mcp import ClientSession
from mcp.client.sse import sse_client

from beeai_framework.tools.mcp import MCPTool


@asynccontextmanager
async def redis_client(redis_url: str) -> AsyncGenerator[redis.Redis, None]:
    client = redis.Redis.from_url(redis_url)
    await client.ping()
    try:
        yield client
    finally:
        await client.aclose()


@asynccontextmanager
async def mcp_tools(
    sse_url: str, filter: Callable[[str], bool] | None = None
) -> AsyncGenerator[list[MCPTool], None]:
    async with sse_client(sse_url) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await MCPTool.from_client(session)
        if filter:
            tools = [t for t in tools if filter(t.name)]
        try:
            yield tools
        finally:
            await MCPTool.cleanup()
