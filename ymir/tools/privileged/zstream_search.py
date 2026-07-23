import json
import logging
import os
import re
from enum import Enum
from urllib.parse import quote, urlparse

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.common.utils import run_tool
from ymir.common.version_utils import is_older_zstream, parse_rhel_version
from ymir.tools.base import CloneableTool as Tool
from ymir.tools.constants import AIOHTTP_TIMEOUT, YMIR_USER_AGENT
from ymir.tools.http import aiohttp_get_with_retries
from ymir.tools.privileged.jira import (
    GetJiraDevStatusTool,
    GetJiraPullRequestsTool,
    SearchJiraIssuesTool,
)

logger = logging.getLogger(__name__)


class ZStreamSearchResult(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    NOT_APPLICABLE = "not_applicable"


class ZStreamSearchToolInput(BaseModel):
    component: str = Field(description="package/component name from the Jira issue (e.g. 'fence-agents')")
    summary: str = Field(
        description="issue summary text (e.g. 'fence_ibm_vpc: fix missing statuses [rhel-9.6.z]')"
    )
    fix_version: str = Field(description="target fix version from the Jira issue (e.g. 'rhel-9.6.z')")


class ZStreamSearchToolResult(BaseModel):
    result: ZStreamSearchResult = Field(description="result of the tool invocation")
    source_issue: str | None = Field(description="Jira issue key where commits were found")
    source_version: str | None = Field(description="fixVersion of the issue where commits were found")
    related_commits: list[str] | None = Field(description="commit/patch URLs to use for backporting")


class ZStreamSearchToolOutput(JSONToolOutput[ZStreamSearchToolResult]):
    pass


def _clean_summary(summary: str) -> str:
    """Remove RHEL version suffix in square brackets from summary text.

    Examples:
        'fence_ibm_vpc: fix missing statuses [rhel-10.0.z]' -> 'fence_ibm_vpc: fix missing statuses'
        'some fix [rhel-9.6.z]' -> 'some fix'
    """
    return re.sub(r"\s*\[rhel-[^\]]+\]\s*$", "", summary).strip()


def _get_patch_url(commit_url: str) -> str:
    """Convert a commit URL to a patch URL where possible.

    For GitLab/GitHub commit URLs, appends .patch to get the raw patch.
    """
    parsed = urlparse(commit_url)
    # Already a patch URL
    if parsed.path.endswith(".patch"):
        return commit_url
    # GitLab commit URL pattern: /-/commit/{hash}
    if "/-/commit/" in parsed.path:
        return commit_url + ".patch"
    # GitHub commit URL pattern: /commit/{hash}
    if "/commit/" in parsed.path and (
        "github" in (parsed.hostname or "") or "gitlab" in (parsed.hostname or "")
    ):
        return commit_url + ".patch"
    return commit_url


def _version_sort_key(
    issue_version: tuple[str, str, bool],
    target_major: str,
    target_minor: int,
) -> tuple[int, int]:
    """Compute sort key for version proximity.

    Returns (priority, distance) where:
    - priority 0 = same major, z-stream
    - priority 1 = same major, y-stream
    - priority 2 = different major
    - distance = abs(minor - target_minor)

    Lower values are closer/preferred.
    """
    major, minor_str, is_zstream = issue_version
    minor = int(minor_str)

    if major != target_major:
        return (2, abs(minor - target_minor))
    if is_zstream:
        return (0, abs(minor - target_minor))
    return (1, abs(minor - target_minor))


async def _fetch_mr_commits(mr_url: str) -> list[str]:
    """Fetch commit patch URLs from a merged GitLab merge request."""
    if not mr_url:
        return []

    match = re.search(
        r"gitlab\.com/(.+?)/-/merge_requests/(\d+)",
        mr_url,
    )
    if not match:
        logger.debug(f"Could not parse MR URL: {mr_url}")
        return []

    project_path = match.group(1)
    mr_iid = match.group(2)
    encoded_project = quote(project_path, safe="")

    api_url = f"https://gitlab.com/api/v4/projects/{encoded_project}/merge_requests/{mr_iid}/commits"

    headers: dict[str, str] = {"User-Agent": YMIR_USER_AGENT}
    if token := os.getenv("GITLAB_TOKEN"):
        headers["PRIVATE-TOKEN"] = token

    try:
        async with (
            aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
            aiohttp_get_with_retries(session, api_url, headers=headers) as response,
        ):
            response.raise_for_status()
            commits = await response.json()
    except Exception as e:
        logger.debug(f"Failed to fetch commits for MR {mr_url}: {e}")
        return []

    urls = []
    for commit in commits:
        sha = commit.get("id", "")
        if sha:
            commit_url = f"https://gitlab.com/{project_path}/-/commit/{sha}"
            urls.append(_get_patch_url(commit_url))

    return urls


async def _get_merged_commits(issue_key: str) -> list[str]:
    """Get commit patch URLs for merged commits linked to a Jira issue.

    Strategy:
    - If merged MRs exist: return commits from those MRs (via GitLab API)
    - If no MRs exist at all: return dev-status commits (direct pushes)
    - If only open/unmerged MRs exist: return nothing (bot branches)
    """
    try:
        pr_result = await run_tool(
            GetJiraPullRequestsTool(),
            issue_key=issue_key,
        )
        pr_data = json.loads(pr_result) if isinstance(pr_result, str) else pr_result
        pull_requests = pr_data.get("pull_requests", [])
    except ToolError:
        raise
    except Exception as e:
        logger.warning(f"Failed to fetch MRs for {issue_key}: {e}")
        return []

    merged_mrs = [pr for pr in pull_requests if pr.get("status") == "MERGED"]

    if merged_mrs:
        logger.info(f"Found {len(merged_mrs)} merged MR(s) for {issue_key}")
        commit_urls: list[str] = []
        for mr in merged_mrs:
            urls = await _fetch_mr_commits(mr.get("url", ""))
            commit_urls.extend(urls)
        return commit_urls

    if pull_requests:
        logger.info(f"Only unmerged MRs for {issue_key} ({len(pull_requests)} open), skipping")
        return []

    # No MRs at all — commits were pushed directly, use dev-status
    logger.info(f"No MRs for {issue_key}, fetching direct-push commits from dev status")
    try:
        dev_status = await run_tool(
            GetJiraDevStatusTool(),
            issue_key=issue_key,
        )
        dev_status_data = json.loads(dev_status) if isinstance(dev_status, str) else dev_status
        commits = dev_status_data.get("commits", [])
    except Exception as e:
        logger.debug(f"Dev status request error for {issue_key}: {e}")
        return []

    return [_get_patch_url(c["url"]) for c in commits if c.get("url")]


class ZStreamSearchTool(Tool[ZStreamSearchToolInput, ToolRunOptions, ZStreamSearchToolOutput]):
    name = "zstream_search"
    description = """
        Search for commits related to an older z-stream backport by finding
        related Jira issues in closer/newer streams and extracting their
        Development section commits.

        Use this tool BEFORE upstream_search when the fix_version targets an
        older z-stream (a z-stream version older than the current z-stream
        for the same RHEL major version).

        If 'found', returns commit URLs as patch sources for backporting.
        If 'not_found', no relevant commits exist or can be located.
        If 'not_applicable', the fix_version is not an older z-stream target.
    """
    input_schema = ZStreamSearchToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "commands", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: ZStreamSearchToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> ZStreamSearchToolOutput:
        def _not_applicable():
            return ZStreamSearchToolOutput(
                ZStreamSearchToolResult(
                    result=ZStreamSearchResult.NOT_APPLICABLE,
                    source_issue=None,
                    source_version=None,
                    related_commits=None,
                )
            )

        def _not_found():
            return ZStreamSearchToolOutput(
                ZStreamSearchToolResult(
                    result=ZStreamSearchResult.NOT_FOUND,
                    source_issue=None,
                    source_version=None,
                    related_commits=None,
                )
            )

        # 1. Check applicability
        parsed = parse_rhel_version(tool_input.fix_version)
        if not parsed:
            logger.info(f"Could not parse fix_version: {tool_input.fix_version}")
            return _not_applicable()

        target_major, target_minor_str, is_zstream_version = parsed
        if not is_zstream_version:
            logger.info(f"fix_version {tool_input.fix_version} is not a z-stream")
            return _not_applicable()

        target_minor = int(target_minor_str)

        try:
            older = await is_older_zstream(tool_input.fix_version)
        except Exception as e:
            raise ToolError(f"Failed to check z-stream status: {e}") from e

        if not older:
            logger.info(f"fix_version {tool_input.fix_version} is not an older z-stream")
            return _not_applicable()

        logger.info(f"Older z-stream detected: {tool_input.fix_version}, searching for related issues")

        # 2. Clean summary
        cleaned_summary = _clean_summary(tool_input.summary)
        logger.info(f"Cleaned summary: '{cleaned_summary}'")

        # 3. Search Jira for related issues
        escaped_component = tool_input.component.replace('"', '\\"')
        escaped_summary = cleaned_summary.replace('"', '\\"')

        jql = (
            f'component = "{escaped_component}" AND summary ~ "{escaped_summary}"'
            f' AND "Fixed in Build" is not EMPTY'
        )
        logger.info(f"Searching Jira with JQL: {jql}")

        try:
            search_result = await run_tool(
                SearchJiraIssuesTool(),
                jql=jql,
                fields=["fixVersions"],
                max_results=50,
            )
            issues = json.loads(search_result) if isinstance(search_result, str) else search_result

            if not issues:
                logger.info("No related issues found in Jira search")
                return _not_found()

            logger.info(f"Found {len(issues)} related issues")

            # 4. Sort by version proximity - only include versions higher than target
            candidates = []
            for issue in issues:
                fix_versions = issue.get("fields", {}).get("fixVersions", [])
                for fv in fix_versions:
                    fv_name = fv.get("name", "")
                    fv_parsed = parse_rhel_version(fv_name)
                    if not fv_parsed:
                        continue
                    fv_major, fv_minor_str, _ = fv_parsed
                    if fv_major == target_major and int(fv_minor_str) > target_minor:
                        sort_key = _version_sort_key(fv_parsed, target_major, target_minor)
                        candidates.append((sort_key, fv_name, issue))

            # Sort by proximity (closest first)
            candidates.sort(key=lambda x: x[0])

            if not candidates:
                logger.info("No candidate issues with newer versions found")
                return _not_found()

            logger.info(f"Cascading through {len(candidates)} candidates: {[c[1] for c in candidates]}")

            # 5. Cascade through candidates - find commits from merged MRs
            for _, version_name, issue in candidates:
                issue_key = issue.get("key", "")
                if not issue_key:
                    continue

                commit_urls = await _get_merged_commits(issue_key)

                if commit_urls:
                    logger.info(
                        f"Found {len(commit_urls)} merged commit(s) in {issue_key} (version {version_name})"
                    )
                    return ZStreamSearchToolOutput(
                        ZStreamSearchToolResult(
                            result=ZStreamSearchResult.FOUND,
                            source_issue=issue_key,
                            source_version=version_name,
                            related_commits=commit_urls,
                        )
                    )

                logger.info(f"No merged commits for {issue_key} (version {version_name}), cascading...")

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Error during Jira search: {e}") from e

        # 6. No commits found in any cascaded issue
        logger.info("No merged commits found in any related issues")
        return _not_found()
