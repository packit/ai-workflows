import logging
import os
from urllib.parse import quote

import aiohttp

from ymir.common.version_utils import parse_branch_name
from ymir.tools.constants import AIOHTTP_TIMEOUT, YMIR_USER_AGENT

logger = logging.getLogger(__name__)

BUGZILLA_DATA_BASE_URL = "https://gitlab.cee.redhat.com/bugzilla-data/components/-/raw/main"


def parse_component_file(text: str) -> dict[str, str]:
    """Parse bugzilla component file (key-value pairs separated by ': ')."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            value = value.strip()
            if value:
                fields[key] = value
    return fields


async def fetch_bugzilla_component_data(
    package: str,
    rhel_major: str,
) -> dict[str, str] | None:
    """Fetch component metadata from the bugzilla-data repo on internal GitLab.

    Returns parsed fields dict or None if the component is not found.
    """
    url = f"{BUGZILLA_DATA_BASE_URL}/RHEL{rhel_major}/{quote(package, safe='')}?ref_type=heads&inline=false"
    logger.debug("Fetching bugzilla component data from RHEL%s/%s", rhel_major, package)
    try:
        async with (
            aiohttp.ClientSession(
                timeout=AIOHTTP_TIMEOUT,
                headers={"User-Agent": YMIR_USER_AGENT},
            ) as session,
            session.get(url) as response,
        ):
            if response.status == 404:
                logger.info(
                    "Component %s not found in bugzilla-data for RHEL%s",
                    package,
                    rhel_major,
                )
                return None
            if response.status >= 400:
                logger.warning(
                    "Failed to fetch bugzilla component data for %s (RHEL%s): HTTP %d",
                    package,
                    rhel_major,
                    response.status,
                )
                return None
            text = await response.text()
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("Error fetching bugzilla component data for %s: %s", package, e)
        return None

    return parse_component_file(text)


async def resolve_gitlab_user_id(email: str) -> int | None:
    """Resolve an email address to a gitlab.com user ID.

    Uses the GitLab users search API, then verifies via the single-user
    endpoint's ``public_email`` field.
    """
    token = os.getenv("GITLAB_TOKEN")
    if not token:
        logger.warning("GITLAB_TOKEN not set — cannot resolve GitLab user for %s", email)
        return None

    headers = {"PRIVATE-TOKEN": token, "User-Agent": YMIR_USER_AGENT}
    user_id = await _search_gitlab_user(email, headers)

    if user_id is None:
        logger.warning("Could not resolve GitLab user for %s", email)
    return user_id


async def _search_gitlab_user(
    query: str,
    headers: dict[str, str],
) -> int | None:
    """Search gitlab.com for a user matching *query* by verified email.

    The list endpoint (GET /users) does not return email fields for non-admin
    tokens, so we fetch each candidate's profile (GET /users/:id) to read
    ``public_email`` and verify the match.
    """
    try:
        async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            async with session.get(
                "https://gitlab.com/api/v4/users",
                headers=headers,
                params={"search": query},
            ) as response:
                if response.status != 200:
                    logger.warning(
                        "GitLab user search returned HTTP %d for %r",
                        response.status,
                        query,
                    )
                    return None
                users = await response.json()

            if not users:
                logger.debug("No GitLab users found for search query %r", query)
                return None

            query_lower = query.lower()
            for user in users:
                user_id = user["id"]
                async with session.get(
                    f"https://gitlab.com/api/v4/users/{user_id}",
                    headers=headers,
                ) as detail_resp:
                    if detail_resp.status != 200:
                        logger.warning(
                            "GitLab user detail returned HTTP %d for user %d",
                            detail_resp.status,
                            user_id,
                        )
                        continue
                    detail = await detail_resp.json()

                public_email = detail.get("public_email", "")
                if public_email and public_email.lower() == query_lower:
                    return user_id

    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("GitLab user search failed for %r: %s", query, e)
        return None

    return None


async def resolve_reviewers(package: str, dist_git_branch: str) -> list[int]:
    """Resolve reviewer GitLab user IDs for a package on a given branch.

    Returns a (possibly empty) list of user IDs. Never raises.
    """
    try:
        parsed = parse_branch_name(dist_git_branch)
        if not parsed:
            logger.warning("Cannot parse branch %s to determine RHEL version", dist_git_branch)
            return []

        rhel_major = parsed[0]
        component_data = await fetch_bugzilla_component_data(package, rhel_major)
        if not component_data:
            return []

        emails: list[str] = []
        if assignee := component_data.get("Default Assignee"):
            emails.append(assignee)
        if qa_contact := component_data.get("QA Contact"):
            emails.append(qa_contact)

        if not emails:
            logger.info("No assignee or QA contact for %s (RHEL%s)", package, rhel_major)
            return []

        reviewer_ids: list[int] = []
        for email in emails:
            user_id = await resolve_gitlab_user_id(email)
            if user_id is not None and user_id not in reviewer_ids:
                reviewer_ids.append(user_id)

        return reviewer_ids

    except Exception:
        logger.warning(
            "Failed to resolve reviewers for %s (%s)",
            package,
            dist_git_branch,
            exc_info=True,
        )
        return []


if __name__ == "__main__":
    import asyncio
    import sys

    logging.basicConfig(level=logging.DEBUG)

    if len(sys.argv) == 2 and "@" in sys.argv[1]:
        email = sys.argv[1]
        user_id = asyncio.run(resolve_gitlab_user_id(email))
        print(f"{email} -> user_id={user_id}")
    elif len(sys.argv) == 3:
        pkg, branch = sys.argv[1], sys.argv[2]
        reviewer_ids = asyncio.run(resolve_reviewers(pkg, branch))
        print(f"Resolved reviewer IDs for {pkg} ({branch}): {reviewer_ids}")
    else:
        print(f"Usage: python -m {__name__} <email>")
        print(f"       python -m {__name__} <package> <dist_git_branch>")
        sys.exit(1)
