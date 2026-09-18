"""Consolidation route module for the Ymir API.

Registers ``POST /api/consolidation`` which submits MR consolidation
jobs into the Redis hash queue.  Also registers the ``consolidate``
command for the Jira webhook command parser.
"""

import argparse
import logging
from typing import Literal

from aiohttp import web
from pydantic import BaseModel, ValidationError, field_validator

from ymir.api import command_parser
from ymir.api.app_keys import REDIS_KEY
from ymir.common.merge_queue import SubmitResult, submit_merge_job

logger = logging.getLogger(__name__)


class ConsolidationRequest(BaseModel):
    """Payload for the POST /api/consolidation endpoint."""

    package: str
    target_branch: str
    source_issues: list[str] | None = None
    release_strategy: Literal["merged", "per_commit"] | None = None

    @field_validator("source_issues")
    @classmethod
    def validate_source_issues_length(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) != 2:
            msg = "source_issues must contain exactly 2 issue keys"
            raise ValueError(msg)
        return v


async def _submit_consolidation_job(
    payload: ConsolidationRequest,
    request: web.Request,
) -> web.Response:
    """Shared submission logic used by both the REST endpoint and the command handler."""
    redis_conn = request.app[REDIS_KEY]

    try:
        result = await submit_merge_job(
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

    if result is SubmitResult.SUBMITTED:
        logger.info(
            "Consolidation job submitted: package=%s branch=%s source_issues=%s strategy=%s",
            payload.package,
            payload.target_branch,
            payload.source_issues,
            payload.release_strategy,
        )
        return web.json_response({"submitted": True}, status=201)

    if result is SubmitResult.CONFLICT:
        return web.json_response(
            {"submitted": False, "reason": "conflict"},
            status=409,
        )

    return web.json_response(
        {"submitted": False, "reason": "already_queued"},
        status=200,
    )


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
        errors = [{k: v for k, v in e.items() if k != "ctx"} for e in exc.errors()]
        return web.json_response(
            {"error": "validation failed", "details": errors},
            status=400,
        )

    return await _submit_consolidation_job(payload, request)


# ---------------------------------------------------------------------------
# Jira webhook command: consolidate <package> <branch> [options]
# ---------------------------------------------------------------------------


def _build_consolidate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="consolidate", exit_on_error=False)
    parser.add_argument("package")
    parser.add_argument("target_branch")
    parser.add_argument("--source-issues", nargs=2, default=None)
    parser.add_argument("--release-strategy", choices=["merged", "per_commit"], default=None)
    return parser


_consolidate_parser = _build_consolidate_parser()


def parse_consolidate_args(args: list[str]) -> ConsolidationRequest:
    """Parse CLI-style consolidate arguments into a request model."""
    ns = _consolidate_parser.parse_args(args)
    return ConsolidationRequest(
        package=ns.package,
        target_branch=ns.target_branch,
        source_issues=ns.source_issues,
        release_strategy=ns.release_strategy,
    )


async def handle_consolidate_command(
    args: list[str],
    request: web.Request,
) -> web.Response:
    """Command handler invoked by the Jira webhook command parser."""
    try:
        payload = parse_consolidate_args(args)
    except (argparse.ArgumentError, SystemExit) as exc:
        return web.json_response(
            {"error": f"invalid consolidate arguments: {exc}"},
            status=400,
        )
    return await _submit_consolidation_job(payload, request)


command_parser.register("consolidate", handle_consolidate_command)


def add_routes(app: web.Application) -> None:
    """Register consolidation routes on the application."""
    app.router.add_post("/api/consolidation", submit_consolidation)
