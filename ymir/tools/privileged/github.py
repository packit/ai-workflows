"""Privileged GitHub MCP tools.

These tools access GitHub API with GITHUB_READONLY_TOKEN authentication.
The token is only available in the MCP gateway process, which has no shell
execution capability - preventing token leakage via prompt injection.

SECURITY:
- Use a fine-grained, expiring token restricted to the required repositories
  with only read permissions. Do not use the classic ``public_repo`` scope:
  it grants write access to public repositories.
- Used solely for rate limiting (60 → 5,000 req/hr)
- Cannot push code, create PRs, or modify GitHub resources
"""

import os
import re
from urllib.parse import urlparse

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.tools.base import CloneableTool as Tool
from ymir.tools.constants import AIOHTTP_TIMEOUT, YMIR_USER_AGENT
from ymir.tools.http import aiohttp_get_with_retries


def _github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": YMIR_USER_AGENT}
    if token := os.getenv("GITHUB_READONLY_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


class GetGithubPullRequestToolInput(BaseModel):
    """Input for fetching GitHub pull request information."""

    pr_url: str = Field(description="GitHub pull request URL (e.g., https://github.com/owner/repo/pull/123)")


class GetGithubPullRequestToolOutput(JSONToolOutput[dict[str, str | bool]]):
    pass


class GetGithubPullRequestTool(
    Tool[GetGithubPullRequestToolInput, ToolRunOptions, GetGithubPullRequestToolOutput]
):
    """Fetch GitHub pull request information using authenticated API.

    Uses GITHUB_READONLY_TOKEN for authentication to avoid rate limiting.
    Token is only accessible in the MCP gateway process (no shell execution).
    """

    name = "get_github_pull_request"
    description = "Fetch GitHub pull request metadata (head SHA, state, merged status)"
    input_schema = GetGithubPullRequestToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "github", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetGithubPullRequestToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> GetGithubPullRequestToolOutput:
        """Fetch PR information from GitHub API."""
        # Extract owner, repo, PR number from URL
        pr_match = re.search(r"github\.com/([\w\-\.]+)/([\w\-\.]+)/pull/(\d+)", tool_input.pr_url)
        if not pr_match:
            raise ToolError(f"Invalid GitHub PR URL: {tool_input.pr_url}")

        owner = pr_match.group(1)
        repo = pr_match.group(2)
        pr_number = pr_match.group(3)

        api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
        headers = _github_headers()

        try:
            async with (
                aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                aiohttp_get_with_retries(session, api_url, headers=headers) as response,
            ):
                response.raise_for_status()
                data = await response.json()

                return GetGithubPullRequestToolOutput(
                    result={
                        "head_sha": data["head"]["sha"],
                        "state": data["state"],
                        "merged": data.get("merged", False),
                    }
                )
        except (aiohttp.ClientError, TimeoutError, KeyError) as e:
            raise ToolError(
                f"Failed to fetch GitHub PR {pr_number} from {owner}/{repo}. "
                f"The PR might be private, deleted, or the API is unavailable. Error: {e}"
            ) from e


class GetGithubCompareToolInput(BaseModel):
    """Input for fetching GitHub compare information."""

    repo_url: str = Field(description="GitHub repository URL (e.g., https://github.com/owner/repo)")
    base_ref: str = Field(description="Base reference (tag, branch, or commit)")
    target_ref: str = Field(description="Target reference (tag, branch, or commit)")


class GetGithubCompareToolOutput(JSONToolOutput[dict[str, list[str]]]):
    pass


class GetGithubCompareTool(Tool[GetGithubCompareToolInput, ToolRunOptions, GetGithubCompareToolOutput]):
    """Fetch commit list between two refs using authenticated GitHub API.

    Uses GITHUB_READONLY_TOKEN for authentication to avoid rate limiting.
    Token is only accessible in the MCP gateway process (no shell execution).
    """

    name = "get_github_compare"
    description = "Fetch list of commits between two Git references (tags, branches, commits)"
    input_schema = GetGithubCompareToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "github", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetGithubCompareToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> GetGithubCompareToolOutput:
        """Fetch compare information from GitHub API."""
        from urllib.parse import quote

        # Extract owner/repo from URL
        repo_match = re.search(r"github\.com/([\w\-\.]+)/([\w\-\.]+)", tool_input.repo_url)
        if not repo_match:
            raise ToolError(f"Invalid GitHub repository URL: {tool_input.repo_url}")

        owner = repo_match.group(1)
        repo = repo_match.group(2).removesuffix(".git")
        project_path = f"{owner}/{repo}"

        api_url = (
            f"https://api.github.com/repos/{project_path}/compare/"
            f"{quote(tool_input.base_ref, safe='')}...{quote(tool_input.target_ref, safe='')}"
        )
        headers = _github_headers()

        try:
            async with (
                aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                aiohttp_get_with_retries(session, api_url, headers=headers) as response,
            ):
                response.raise_for_status()
                data = await response.json()

                # GitHub returns commits oldest-first
                commits = [
                    commit.get("sha") for commit in data.get("commits", []) if commit and commit.get("sha")
                ]

                return GetGithubCompareToolOutput(result={"commits": commits})
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ToolError(
                f"Failed to fetch GitHub compare {tool_input.base_ref}...{tool_input.target_ref} "
                f"for {project_path}. Error: {e}"
            ) from e


class GetGithubPatchToolInput(BaseModel):
    """Input for fetching a patch hosted on GitHub."""

    patch_url: str = Field(description="HTTPS URL for a patch, diff, or other patch source hosted on GitHub")


class GetGithubPatchTool(Tool[GetGithubPatchToolInput, ToolRunOptions, StringToolOutput]):
    """Fetch a GitHub-hosted patch through authenticated GitHub access."""

    name = "get_github_patch"
    description = "Fetch a GitHub-hosted patch or diff using authenticated API access"
    input_schema = GetGithubPatchToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "github", self.name], creator=self)

    async def _run(
        self,
        tool_input: GetGithubPatchToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        parsed_url = urlparse(tool_input.patch_url)
        if parsed_url.scheme != "https" or parsed_url.hostname not in {"github.com", "www.github.com"}:
            raise ToolError(f"Invalid GitHub patch URL: {tool_input.patch_url}")

        patch_match = re.fullmatch(
            r"/([\w.-]+)/([\w.-]+)/(?:pull/(\d+)|commit/([0-9a-fA-F]+))\.(patch|diff)",
            parsed_url.path,
        )
        if patch_match:
            owner, repo, pull_number, commit, patch_format = patch_match.groups()
            if pull_number:
                request_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}"
            else:
                request_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit}"
            headers = _github_headers(f"application/vnd.github.{patch_format}")
        else:
            # Retain support for GitHub-hosted patch sources that do not map to
            # a PR or commit API endpoint, such as raw files and release assets.
            request_url = tool_input.patch_url
            headers = _github_headers("*/*")

        try:
            async with (
                aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                aiohttp_get_with_retries(
                    session,
                    request_url,
                    headers=headers,
                ) as response,
            ):
                response.raise_for_status()
                return StringToolOutput(result=await response.text())
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ToolError(f"Failed to fetch GitHub patch from {tool_input.patch_url}. Error: {e}") from e
