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
):
    """
    Searches for merge requests in a GitLab project related to an issue key.

    This function queries the GitLab API and yields MergeRequest objects
    for each MR found that matches the search criteria.

    Args:
        project (str): The path of the GitLab project (e.g., 'redhat/centos-stream/rpms/podman').
        issue_key (str): The issue key to search for (e.g., 'RHEL-12345').
        state (MergeRequestState | None, optional): If provided, filters MRs
        by their state (e.g., 'opened', 'merged'). Defaults to None.

    Yields:
        MergeRequest: A data object for each matching merge request.
    """
    logger.debug("Searching for MRs for %s in %s", issue_key, project)
    path = f"projects/{urlquote(project, safe='')}/merge_requests"

    params = {"search": issue_key, "view": "simple"}
    if state is not None:
        params["state"] = state

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
