"""Consolidation route module for the Ymir API.

Registers ``POST /api/consolidation`` which submits MR consolidation
jobs into the Redis hash queue.
"""

from __future__ import annotations

import logging

from aiohttp import web
from pydantic import BaseModel, ValidationError

from ymir.api.server import REDIS_KEY
from ymir.common.base_utils import fix_await
from ymir.common.constants import RedisQueues
from ymir.common.merge_queue import _consolidation_field_key, submit_merge_job

logger = logging.getLogger(__name__)

_CONSOLIDATION_HASH_KEY = RedisQueues.MERGE_CONSOLIDATION_QUEUE.value


class ConsolidationRequest(BaseModel):
    """Payload for the POST /api/consolidation endpoint."""

    package: str
    target_branch: str
    source_issues: list[str] | None = None
    release_strategy: str | None = None


async def submit_consolidation(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid JSON body"},
            status=400,
        )

    try:
        payload = ConsolidationRequest.model_validate(body)
    except ValidationError as exc:
        return web.json_response(
            {"error": "validation failed", "details": exc.errors()},
            status=400,
        )

    redis_conn = request.app[REDIS_KEY]

    # Label-triggered mode: check for conflicts before submitting, because
    # submit_merge_job() silently drops source_issues when a pending job
    # already exists for the same package/branch.
    if payload.source_issues is not None:
        pending_key = _consolidation_field_key(payload.package, payload.target_branch, "pending")
        active_key = _consolidation_field_key(payload.package, payload.target_branch, "active")
        existing_pending = await fix_await(redis_conn.hget(_CONSOLIDATION_HASH_KEY, pending_key))
        existing_active = await fix_await(redis_conn.hget(_CONSOLIDATION_HASH_KEY, active_key))
        if existing_pending is not None or existing_active is not None:
            return web.json_response(
                {"submitted": False, "reason": "conflict"},
                status=409,
            )

    try:
        submitted = await submit_merge_job(
            redis_conn,
            payload.package,
            payload.target_branch,
            source_issues=payload.source_issues,
            release_strategy=payload.release_strategy,
        )
    except Exception:
        logger.exception("Redis error while submitting consolidation job")
        return web.json_response(
            {"error": "internal server error"},
            status=500,
        )

    if submitted:
        return web.json_response({"submitted": True}, status=201)

    return web.json_response(
        {"submitted": False, "reason": "already_queued"},
        status=200,
    )


def add_routes(app: web.Application) -> None:
    """Register consolidation routes on the application."""
    app.router.add_post("/api/consolidation", submit_consolidation)
