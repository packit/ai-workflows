"""Shared, host-validated GitLab REST API helpers."""

import os
from typing import Any
from urllib.parse import quote, unquote, urlparse

from ymir.common.constants import (
    ALLOWED_GITLAB_HOSTS,
    GITLAB_REDHAT_API_PREFIX,
    GITLAB_REDHAT_WEB_PREFIX,
    GITLAB_URL,
    YMIR_USER_AGENT,
)
from ymir.common.http_utils import aiohttp_api_get


def is_private_gitlab(url: str) -> bool:
    """Return whether a GitLab URL is in a namespace requiring Red Hat auth."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname == "gitlab.cee.redhat.com":
        return True
    if hostname != "gitlab.com":
        return False
    if parsed.path.startswith(GITLAB_REDHAT_WEB_PREFIX) or parsed.path.startswith(GITLAB_REDHAT_API_PREFIX):
        return True
    fork_namespace = os.getenv("FORK_NAMESPACE", "").strip("/")
    return bool(fork_namespace and parsed.path.startswith(f"/{fork_namespace}/"))


def gitlab_headers(url: str) -> dict[str, str]:
    """Build tool-style GitLab headers, limiting credentials to private RH paths."""
    headers = {"User-Agent": YMIR_USER_AGENT}
    if is_private_gitlab(url) and (token := os.getenv("GITLAB_TOKEN")):
        headers["PRIVATE-TOKEN"] = token
    return headers


def gitlab_api_url(path: str, *, gitlab_url: str = GITLAB_URL) -> str:
    """Build an API URL after validating its host against the sweep allowlist."""
    hostname = urlparse(gitlab_url).hostname
    if hostname not in ALLOWED_GITLAB_HOSTS:
        raise ValueError(
            f"Refusing to call GitLab API on untrusted host {hostname!r}; "
            f"allowed hosts: {sorted(ALLOWED_GITLAB_HOSTS)}"
        )
    return f"{gitlab_url.rstrip('/')}/api/v4/{path.lstrip('/')}"


async def gitlab_api_get(
    path: str,
    *,
    params: dict | None = None,
    gitlab_url: str = GITLAB_URL,
    session=None,
) -> Any:
    """GET JSON from a trusted GitLab host using the shared async retry policy."""
    url = gitlab_api_url(path, gitlab_url=gitlab_url)
    auth_path = unquote(path.lstrip("/"))
    if auth_path.startswith("projects/"):
        auth_path = auth_path.removeprefix("projects/").split("/merge_requests", 1)[0]
    auth_url = f"{gitlab_url.rstrip('/')}/{auth_path}"
    return await aiohttp_api_get(url, params=params, headers=gitlab_headers(auth_url), session=session)


async def search_gitlab_project_mrs(
    project: str,
    search: str,
    *,
    state: str | None = None,
    gitlab_url: str = GITLAB_URL,
    session=None,
) -> list[dict[str, Any]]:
    """Search a project's MRs and normalize response keys for consumers."""
    path = f"projects/{quote(project, safe='')}/merge_requests"
    params: dict[str, str] = {"search": search}
    if state is not None:
        params["state"] = state
    data = await gitlab_api_get(path, params=params, gitlab_url=gitlab_url, session=session)
    return [
        {
            "project": project,
            "iid": mr["iid"],
            "url": mr["web_url"],
            "title": mr["title"],
            "description": mr.get("description", ""),
            "state": mr["state"],
            "merged_at": mr.get("merged_at"),
        }
        for mr in data
    ]
