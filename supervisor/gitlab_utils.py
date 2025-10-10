from functools import cache
from urllib.parse import quote as urlquote
import logging
import os
from typing import Any

from .http_utils import requests_session
from .supervisor_types import MergeRequest, MergeRequestState

logger = logging.getLogger(__name__)

GITLAB_URL = "https://gitlab.com"


@cache
def gitlab_headers() -> dict[str, str]:
    gitlab_token = os.environ["GITLAB_TOKEN"]

    return {
        "Authorization": f"Bearer {gitlab_token}",
        "Content-Type": "application/json",
    }


def gitlab_api_get(path: str, *, params: dict | None = None) -> Any:
    url = f"{GITLAB_URL}/api/v4/{path}"
    response = requests_session().get(url, headers=gitlab_headers(), params=params)
    response.raise_for_status()
    return response.json()


def search_gitlab_project_mrs(
    project: str,
    issue_key: str,
    *,
    state: MergeRequestState | None = None,
    simple: bool = True,
):
    logger.debug("Searching for MRs for %s in %s", issue_key, project)
    path = f"projects/{urlquote(project, safe='')}/merge_requests"

    params = {"search": issue_key}
    if state is not None:
        params["state"] = state
    if simple:
        params["view"] = "simple"

    result = gitlab_api_get(path, params=params)

    for mr in result:
        yield MergeRequest(
            project=project,
            iid=mr["iid"],
            url=mr["web_url"],
            title=mr["title"],
            state=mr["state"],
            description=mr["description"],
        )
