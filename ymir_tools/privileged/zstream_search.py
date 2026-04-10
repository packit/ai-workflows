import json
import logging
import re
from enum import Enum
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions, ToolError

from ymir_common.utils import run_tool
from ymir_common.version_utils import parse_rhel_version, is_older_zstream
from ymir_tools.privileged.jira_tools import (
    GetJiraDevStatusTool,
    SearchJiraIssuesTool,
)

logger = logging.getLogger(__name__)


class ZStreamSearchResult(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    NOT_APPLICABLE = "not_applicable"


class ZStreamSearchToolInput(BaseModel):
    component: str = Field(
        description="package/component name from the Jira issue (e.g. 'fence-agents')")
    summary: str = Field(
        description="issue summary text (e.g. 'fence_ibm_vpc: fix missing statuses [rhel-9.6.z]')")
    fix_version: str = Field(
        description="target fix version from the Jira issue (e.g. 'rhel-9.6.z')")


class ZStreamSearchToolResult(BaseModel):
    result: ZStreamSearchResult = Field(
        description="result of the tool invocation")
    source_issue: str | None = Field(
        description="Jira issue key where commits were found")
    source_version: str | None = Field(
        description="fixVersion of the issue where commits were found")
    related_commits: list[str] | None = Field(
        description="commit/patch URLs to use for backporting")


class ZStreamSearchToolOutput(JSONToolOutput[ZStreamSearchToolResult]):
    pass


def _clean_summary(summary: str) -> str:
    """Remove RHEL version suffix in square brackets from summary text.

    Examples:
        'fence_ibm_vpc: fix missing statuses [rhel-10.0.z]' -> 'fence_ibm_vpc: fix missing statuses'
        'some fix [rhel-9.6.z]' -> 'some fix'
    """
    return re.sub(r'\s*\[rhel-[^\]]+\]\s*$', '', summary).strip()


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
        "github" in (parsed.hostname or "")
        or "gitlab" in (parsed.hostname or "")
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
        self, tool_input: ZStreamSearchToolInput,
        options: ToolRunOptions | None, context: RunContext
    ) -> ZStreamSearchToolOutput:
        def _not_applicable():
            return ZStreamSearchToolOutput(ZStreamSearchToolResult(
                result=ZStreamSearchResult.NOT_APPLICABLE,
                source_issue=None,
                source_version=None,
                related_commits=None,
            ))

        def _not_found():
            return ZStreamSearchToolOutput(ZStreamSearchToolResult(
                result=ZStreamSearchResult.NOT_FOUND,
                source_issue=None,
                source_version=None,
                related_commits=None,
            ))

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
            raise ToolError(f"Failed to check z-stream status: {e}")

        if not older:
            logger.info(
                f"fix_version {tool_input.fix_version} is not an older z-stream"
            )
            return _not_applicable()

        logger.info(
            f"Older z-stream detected: {tool_input.fix_version}, "
            f"searching for related issues"
        )

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

            logger.info(
                f"Cascading through {len(candidates)} candidates: "
                f"{[c[1] for c in candidates]}"
            )

            # 5. Cascade through candidates - check Development section for commits
            for sort_key, version_name, issue in candidates:
                issue_key = issue.get("key", "")
                if not issue_key:
                    continue

                try:
                    dev_status = await run_tool(
                        GetJiraDevStatusTool(),
                        issue_key=issue_key,
                    )
                    commits = json.loads(dev_status) if isinstance(dev_status, str) else dev_status
                except Exception as e:
                    logger.debug(f"Dev status request error for {issue_key}: {e}")
                    continue

                commit_urls = [
                    _get_patch_url(c["url"]) for c in commits if c.get("url")
                ]

                if commit_urls:
                    logger.info(
                        f"Found {len(commit_urls)} commits in {issue_key} "
                        f"(version {version_name})"
                    )
                    return ZStreamSearchToolOutput(ZStreamSearchToolResult(
                        result=ZStreamSearchResult.FOUND,
                        source_issue=issue_key,
                        source_version=version_name,
                        related_commits=commit_urls,
                    ))

                logger.info(
                    f"No commits in Development section for {issue_key} "
                    f"(version {version_name}), cascading..."
                )

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Error during Jira search: {e}")

        # 6. No commits found in any cascaded issue
        logger.info("No commits found in any related issue's Development section")
        return _not_found()
