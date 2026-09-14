"""Jira webhook route module for the Ymir API.

Registers ``POST /api/jira/webhook`` which receives Jira webhook
payloads, detects bot-mention commands in comment bodies, and
dispatches them via the command parser.

Jira Cloud delivers ``comment.body`` as an Atlassian Document Format
(ADF) JSON object.  The handler walks the ADF tree looking for a
``mention`` inline node whose ``attrs.id`` matches the configured bot
account, then collects trailing text from the same paragraph as the
command string.
"""

import asyncio
import hmac
import json
import logging
import os

from aiohttp import web

from ymir.api import command_parser, jira_reply

logger = logging.getLogger(__name__)

_WEBHOOK_SECRET_HEADER = "X-Webhook-Secret"  # noqa: S105  # pragma: allowlist secret


def _get_bot_account_id() -> str | None:
    """Return the configured bot Jira account ID, or None."""
    return os.environ.get("JIRA_BOT_ACCOUNT_ID")


def _extract_command_from_adf(adf_body: dict, bot_account_id: str) -> str | None:
    """Extract the command text following a bot mention in an ADF document.

    Walks top-level block nodes looking for a ``mention`` inline node
    whose ``attrs.id`` matches *bot_account_id*.  All ``text`` nodes
    that follow the mention **within the same block** are concatenated
    and returned as the command string.
    """
    for block in adf_body.get("content", []):
        inline_nodes = block.get("content") or []
        for idx, node in enumerate(inline_nodes):
            if node.get("type") == "mention" and (node.get("attrs") or {}).get("id") == bot_account_id:
                text_parts = [
                    subsequent.get("text", "")
                    for subsequent in inline_nodes[idx + 1 :]
                    if subsequent.get("type") == "text"
                ]
                command_text = "".join(text_parts).strip()
                return command_text if command_text else None
    return None


def _extract_command(comment_body: dict, bot_account_id: str) -> str | None:
    """Extract command text from a Jira Cloud ADF comment body."""
    if not isinstance(comment_body, dict):
        logger.warning("Unexpected comment body type %s, expected ADF dict", type(comment_body).__name__)
        return None
    return _extract_command_from_adf(comment_body, bot_account_id)


async def jira_webhook(request: web.Request) -> web.Response:
    expected_secret = os.environ.get("JIRA_WEBHOOK_SECRET")
    if not expected_secret:
        logger.error("JIRA_WEBHOOK_SECRET is not configured, rejecting request")
        return web.json_response({"error": "webhook secret not configured"}, status=500)

    provided = request.headers.get(_WEBHOOK_SECRET_HEADER, "")
    if not hmac.compare_digest(provided, expected_secret):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    event = body.get("webhookEvent", "")
    if event != "comment_created":
        return web.json_response({"ignored": True, "reason": f"event: {event}"})

    comment_body = (body.get("comment") or {}).get("body")
    if not comment_body:
        return web.json_response({"ignored": True, "reason": "empty comment body"})

    bot_account_id = _get_bot_account_id()
    if bot_account_id is None:
        logger.warning("JIRA_BOT_ACCOUNT_ID not configured, ignoring webhook")
        return web.json_response({"ignored": True, "reason": "bot account not configured"})

    command_text = _extract_command(comment_body, bot_account_id)
    if command_text is None:
        return web.json_response({"ignored": True, "reason": "no bot mention"})

    issue_key = (body.get("issue") or {}).get("key")
    response = await command_parser.dispatch(command_text, request)

    if response.status >= 400 and issue_key:
        try:
            error_body = json.loads(response.body)
            message = f"Command failed: {error_body.get('error', 'unknown error')}"
        except Exception:
            message = f"Command failed (HTTP {response.status})"
        asyncio.create_task(  # noqa: RUF006
            jira_reply.post_comment(issue_key, message),
        )

    return response


def add_routes(app: web.Application) -> None:
    """Register Jira webhook routes on the application."""
    app.router.add_post("/api/jira/webhook", jira_webhook)
