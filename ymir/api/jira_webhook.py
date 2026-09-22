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
import hashlib
import hmac
import json
import logging
import os

import aiohttp
from aiohttp import web

from ymir.api import command_parser, jira_reply
from ymir.common.base_utils import get_jira_auth_headers

logger = logging.getLogger(__name__)

# Matches the value used by ymir/tools/privileged/jira.py and
# ymir/jira_issue_fetcher/jira_issue_fetcher.py.  Duplicated here to
# avoid a cross-layer import from the privileged tools package.
_RH_EMPLOYEE_GROUP = "Red Hat Employee"

_SIGNATURE_HEADER = "X-Hub-Signature"  # pragma: allowlist secret

_background_tasks: set[asyncio.Task] = set()


def _verify_signature(raw_body: bytes, secret: str, signature_header: str) -> bool:
    """Verify the HMAC signature sent by Jira Cloud.

    Jira Cloud sends ``X-Hub-Signature: <method>=<hex-digest>`` where
    the digest is an HMAC of the raw request body keyed with the
    webhook secret.  Currently only ``sha256`` is used.
    """
    if "=" not in signature_header:
        return False
    method, given_hex = signature_header.split("=", 1)
    if method != "sha256":
        logger.warning("Unsupported HMAC method %r in X-Hub-Signature", method)
        return False
    calculated = hmac.new(
        secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(calculated, given_hex)


def _get_bot_account_id() -> str | None:
    """Return the configured bot Jira account ID, or None."""
    return os.environ.get("JIRA_BOT_ACCOUNT_ID")


async def _is_rh_employee(account_id: str) -> bool:
    """Check whether a Jira account belongs to the Red Hat Employee group.

    Calls the Jira REST API ``GET /rest/api/3/user?expand=groups`` and
    looks for the ``Red Hat Employee`` group in the response.

    Returns ``False`` on any error (fail closed), consistent with the
    fetcher's ``_label_added_by_rh_employee`` approach.
    """
    jira_url = os.environ.get("JIRA_URL")
    if not jira_url:
        logger.warning("JIRA_URL not configured, cannot verify employee status")
        return False

    url = f"{jira_url.rstrip('/')}/rest/api/3/user"
    try:
        headers = get_jira_auth_headers()
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url,
                params={"accountId": account_id, "expand": "groups"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            if resp.status >= 400:
                logger.warning(
                    "Jira user lookup for %s returned HTTP %d",
                    account_id,
                    resp.status,
                )
                return False
            user_data = await resp.json()
    except Exception:
        logger.exception("Failed to verify employee status for %s", account_id)
        return False

    return any(
        group.get("name") == _RH_EMPLOYEE_GROUP for group in user_data.get("groups", {}).get("items", [])
    )


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

    raw_body = await request.read()
    signature_header = request.headers.get(_SIGNATURE_HEADER, "")
    if not _verify_signature(raw_body, expected_secret, signature_header):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = json.loads(raw_body)
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

    comment_author_id = (body.get("comment") or {}).get("author", {}).get("accountId")
    try:
        is_employee = bool(comment_author_id) and await _is_rh_employee(comment_author_id)
    except Exception:
        logger.exception("Employee verification failed for %s, rejecting (fail closed)", comment_author_id)
        is_employee = False
    if not is_employee:
        logger.warning(
            "Comment author %s is not a verified Red Hat employee, rejecting command",
            comment_author_id,
        )
        return web.json_response(
            {"error": "comment author is not a Red Hat employee"},
            status=403,
        )

    issue_key = (body.get("issue") or {}).get("key")
    response = await command_parser.dispatch(command_text, request)

    if response.status >= 400 and issue_key:
        try:
            error_body = json.loads(response.body)
            message = f"Command failed: {error_body.get('error', 'unknown error')}"
        except Exception:
            message = f"Command failed (HTTP {response.status})"
        task = asyncio.create_task(
            jira_reply.post_comment(issue_key, message),
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return response


def add_routes(app: web.Application) -> None:
    """Register Jira webhook routes on the application."""
    app.router.add_post("/api/jira/webhook", jira_webhook)
