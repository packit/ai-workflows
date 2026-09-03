"""Lightweight Jira comment helper for the Ymir API.

Provides a fire-and-forget ``post_comment()`` coroutine that posts a
comment to a Jira issue.  Designed to be scheduled via
``asyncio.create_task`` so the webhook handler can return immediately.
"""

import logging
import os

import aiohttp

from ymir.common.base_utils import get_jira_auth_headers

logger = logging.getLogger(__name__)


async def post_comment(issue_key: str, message: str) -> None:
    """Post a comment to a Jira issue.  Logs errors, never raises."""
    jira_url = os.environ.get("JIRA_URL")
    if not jira_url:
        logger.warning("JIRA_URL not configured, skipping comment on %s", issue_key)
        return

    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{issue_key}/comment"
    try:
        headers = get_jira_auth_headers()
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, json={"body": message}, headers=headers) as resp,
        ):
            if resp.status >= 400:
                body = await resp.text()
                logger.error(
                    "Failed to post comment on %s (HTTP %d): %s",
                    issue_key,
                    resp.status,
                    body,
                )
            else:
                logger.info("Posted error comment on %s", issue_key)
    except Exception:
        logger.exception("Error posting comment on %s", issue_key)
