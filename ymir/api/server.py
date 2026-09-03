"""Ymir API server.

A lightweight aiohttp service providing HTTP endpoints for submitting
jobs to Ymir's Redis queues. Route modules register their handlers
via ``add_routes(app)`` callables.
"""

import logging
import os

from aiohttp import web

from ymir.api.app_keys import REDIS_KEY
from ymir.api.consolidation import add_routes as add_consolidation_routes
from ymir.api.jira_webhook import add_routes as add_jira_webhook_routes
from ymir.common.base_utils import redis_client
from ymir.common.logging_setup import configure_logging

logger = logging.getLogger(__name__)

_REDIS_CTX_KEY = web.AppKey("redis_ctx")


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app(redis_conn=None) -> web.Application:
    """Build the aiohttp Application.

    When *redis_conn* is provided (e.g. in tests) it is used directly;
    otherwise the app opens its own connection on startup.
    """
    app = web.Application()
    app.router.add_get("/healthz", healthz)

    add_consolidation_routes(app)
    add_jira_webhook_routes(app)

    if redis_conn is not None:
        app[REDIS_KEY] = redis_conn
    else:

        async def on_startup(app: web.Application) -> None:
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            app[_REDIS_CTX_KEY] = redis_client(redis_url)
            app[REDIS_KEY] = await app[_REDIS_CTX_KEY].__aenter__()

        async def on_cleanup(app: web.Application) -> None:
            ctx = app.get(_REDIS_CTX_KEY)
            if ctx is not None:
                await ctx.__aexit__(None, None, None)

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)

    return app


def main() -> None:
    configure_logging(level=logging.INFO)

    host = os.environ.get("API_HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("API_PORT", "8080"))
    logger.info("Starting Ymir API on %s:%d", host, port)
    web.run_app(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
