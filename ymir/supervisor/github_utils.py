"""GitHub REST API helpers for the supervisor and sweep layers.

Authentication is optional: if ``GITHUB_READONLY_TOKEN`` is set it is sent
as a Bearer token to avoid rate limiting (5,000 req/hr vs 60 unauthenticated).

Token Requirements:
- Scope: public_repo (read-only access to public repositories)
- Purpose: Rate limiting only - no write operations

Without a token the client still works for public repositories, which
should cover most upstream open-source projects that Ymir tracks.
"""

import logging
import os
from typing import Any

from ymir.supervisor.http_utils import requests_session

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_READONLY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_api_get(path: str, *, params: dict | None = None) -> Any:
    url = f"{GITHUB_API_URL}/{path}"
    response = requests_session().get(url, headers=_github_headers(), params=params)
    response.raise_for_status()
    return response.json()
