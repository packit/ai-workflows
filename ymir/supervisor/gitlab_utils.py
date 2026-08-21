import logging
import os
from functools import cache
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

from .http_utils import requests_session
from .supervisor_types import MergeRequest, MergeRequestState

logger = logging.getLogger(__name__)

GITLAB_URL = "https://gitlab.com"

# GitLab hosts Ymir is allowed to talk to.  ``gitlab_api_get`` attaches the
# ``GITLAB_TOKEN`` Bearer credential to every request, so the target host must
# be constrained to trusted instances -- otherwise an attacker-controlled host
# (e.g. supplied through an LLM-authored ``blocker_reference``) would receive
# the token.  This is the single source of truth for that allowlist.
ALLOWED_GITLAB_HOSTS = frozenset({"gitlab.com", "gitlab.cee.redhat.com"})


@cache
def gitlab_headers() -> dict[str, str]:
    gitlab_token = os.environ["GITLAB_TOKEN"]

    return {
        "Authorization": f"Bearer {gitlab_token}",
        "Content-Type": "application/json",
    }


def gitlab_api_get(path: str, *, params: dict | None = None, gitlab_url: str = GITLAB_URL) -> Any:
    hostname = urlparse(gitlab_url).hostname
    if hostname not in ALLOWED_GITLAB_HOSTS:
        # Never attach the Bearer token to an untrusted host.
        raise ValueError(
            f"Refusing to call GitLab API on untrusted host {hostname!r}; "
            f"allowed hosts: {sorted(ALLOWED_GITLAB_HOSTS)}"
        )
    url = f"{gitlab_url}/api/v4/{path}"
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

    params = {"search": issue_key}
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
            merged_at=mr["merged_at"],
        )
