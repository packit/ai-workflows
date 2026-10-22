"""Shared GitHub REST API helpers for sweep and tool clients."""

import os
from typing import Any

from ymir.common.constants import GITHUB_API_URL, YMIR_USER_AGENT
from ymir.common.http_utils import aiohttp_api_get


def github_headers(
    accept: str = "application/vnd.github+json",
    *,
    allow_token: bool = True,
) -> dict[str, str]:
    """Build GitHub headers, optionally using the read-only rate-limit token."""
    headers = {"Accept": accept, "User-Agent": YMIR_USER_AGENT}
    token = os.environ.get("GITHUB_READONLY_TOKEN") if allow_token else None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def github_api_get(
    path: str,
    *,
    params: dict | None = None,
    allow_token: bool = True,
    session=None,
) -> Any:
    """GET JSON from GitHub with the common async retry behavior."""
    url = f"{GITHUB_API_URL}/{path.lstrip('/')}"
    return await aiohttp_api_get(
        url,
        params=params,
        headers=github_headers(allow_token=allow_token),
        session=session,
    )
