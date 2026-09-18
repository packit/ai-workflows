"""Shared aiohttp AppKey constants for the Ymir API."""

import redis.asyncio
from aiohttp import web

REDIS_KEY = web.AppKey("redis", redis.asyncio.Redis)
