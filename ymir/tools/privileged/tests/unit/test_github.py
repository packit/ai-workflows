"""Unit tests for privileged GitHub MCP tools."""

from contextlib import asynccontextmanager
from json import JSONDecodeError

import aiohttp
import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError
from flexmock import flexmock

from ymir.tools.privileged.github import (
    MAX_GITHUB_PATCH_PREVIEW_LENGTH,
    GetGithubCompareTool,
    GetGithubCompareToolInput,
    GetGithubPatchFullTool,
    GetGithubPatchTool,
    GetGithubPatchToolInput,
    GetGithubPullRequestTool,
    GetGithubPullRequestToolInput,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _async_return(value):
    return value


def _mock_aiohttp_get(json_data=None, text_data="", status=200):
    """Mock aiohttp.ClientSession.get to return json_data.

    Returns (captured_urls, captured_headers) tuple.
    """
    captured_urls = []
    captured_headers = []

    @asynccontextmanager
    async def fake_get(url, **kwargs):
        captured_urls.append(url)
        captured_headers.append(kwargs.get("headers", {}))
        yield flexmock(
            json=lambda: _async_return(json_data),
            text=lambda: _async_return(text_data),
            raise_for_status=lambda: None,
            status=status,
        )

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(fake_get)
    return captured_urls, captured_headers


def _mock_aiohttp_get_error(error_msg="error"):
    """Mock aiohttp.ClientSession.get to raise aiohttp.ClientError."""

    @asynccontextmanager
    async def fake_get(url, **kwargs):
        raise aiohttp.ClientError(error_msg)
        yield

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(fake_get)


def _mock_aiohttp_get_malformed_json():
    """Mock aiohttp.ClientSession.get to fail while decoding its JSON response."""

    async def malformed_json():
        raise JSONDecodeError("Expecting value", "not json", 0)

    @asynccontextmanager
    async def fake_get(url, **kwargs):
        yield flexmock(
            json=malformed_json,
            raise_for_status=lambda: None,
            status=200,
        )

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(fake_get)


# ---------------------------------------------------------------------------
# GetGithubPatchTool
# ---------------------------------------------------------------------------


class TestGetGithubPatchTool:
    @pytest.fixture
    def tool(self):
        return GetGithubPatchTool(options={"working_directory": None})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("patch_url", "request_url", "accept"),
        [
            (
                "https://github.com/owner/repo/pull/42.patch?download=1",
                "https://api.github.com/repos/owner/repo/pulls/42",
                "application/vnd.github.patch",
            ),
            (
                "https://github.com/owner/repo/commit/98599f6d.diff#patch",
                "https://api.github.com/repos/owner/repo/commits/98599f6d",
                "application/vnd.github.diff",
            ),
            (
                "https://github.com/owner/repo/pull/42/",
                "https://api.github.com/repos/owner/repo/pulls/42",
                "application/vnd.github.patch",
            ),
            (
                "https://github.com/owner/repo/commit/98599f6d",
                "https://api.github.com/repos/owner/repo/commits/98599f6d",
                "application/vnd.github.patch",
            ),
        ],
    )
    async def test_fetch_standard_patch_with_authentication(
        self, tool, monkeypatch, patch_url, request_url, accept
    ):
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_patch_token")  # pragma: allowlist secret
        captured_urls, captured_headers = _mock_aiohttp_get(text_data="diff --git a/file b/file\n")

        result = await tool.run(input=GetGithubPatchToolInput(patch_url=patch_url)).middleware(
            GlobalTrajectoryMiddleware(pretty=True)
        )

        assert result.result == "diff --git a/file b/file\n"
        assert captured_urls == [request_url]
        assert captured_headers[0]["Accept"] == accept
        assert captured_headers[0]["Authorization"] == "Bearer test_patch_token"

    @pytest.mark.asyncio
    async def test_fetch_other_github_patch_source_with_authentication(self, tool, monkeypatch):
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_patch_token")  # pragma: allowlist secret
        captured_urls, captured_headers = _mock_aiohttp_get(text_data="patch contents")

        result = await tool.run(
            input=GetGithubPatchToolInput(
                patch_url="https://www.github.com/owner/repo/raw/main/fix.patch?download=1"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert result.result == "patch contents"
        assert captured_urls == ["https://www.github.com/owner/repo/raw/main/fix.patch?download=1"]
        assert captured_headers[0]["Accept"] == "*/*"
        assert captured_headers[0]["Authorization"] == "Bearer test_patch_token"

    @pytest.mark.asyncio
    async def test_large_patch_is_truncated_for_llm_preview(self, tool):
        patch_content = "x" * (MAX_GITHUB_PATCH_PREVIEW_LENGTH + 1)
        _mock_aiohttp_get(text_data=patch_content)

        result = await tool.run(
            input=GetGithubPatchToolInput(patch_url="https://github.com/owner/repo/pull/42.patch")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert result.result.startswith("x" * MAX_GITHUB_PATCH_PREVIEW_LENGTH)
        assert f"of {len(patch_content)} total" in result.result

    @pytest.mark.asyncio
    async def test_invalid_patch_url(self, tool):
        with pytest.raises(ToolError, match="Invalid GitHub patch URL"):
            await tool.run(
                input=GetGithubPatchToolInput(patch_url="http://github.com/owner/repo/pull/42.patch")
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))


class TestGetGithubPatchFullTool:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("patch_url", "request_url"),
        [
            (
                "https://github.com/owner/repo/pull/42",
                "https://api.github.com/repos/owner/repo/pulls/42",
            ),
            (
                "https://github.com/owner/repo/commit/98599f6d/",
                "https://api.github.com/repos/owner/repo/commits/98599f6d",
            ),
        ],
    )
    async def test_bare_url_returns_full_patch_for_deterministic_application(self, patch_url, request_url):
        patch_content = "x" * (MAX_GITHUB_PATCH_PREVIEW_LENGTH + 1)
        captured_urls, captured_headers = _mock_aiohttp_get(text_data=patch_content)
        tool = GetGithubPatchFullTool(options={"working_directory": None})

        result = await tool.run(input=GetGithubPatchToolInput(patch_url=patch_url)).middleware(
            GlobalTrajectoryMiddleware(pretty=True)
        )

        assert result.result == patch_content
        assert captured_urls == [request_url]
        assert captured_headers[0]["Accept"] == "application/vnd.github.patch"


# ---------------------------------------------------------------------------
# GetGithubPullRequestTool
# ---------------------------------------------------------------------------


class TestGetGithubPullRequestTool:
    @pytest.fixture
    def tool(self):
        return GetGithubPullRequestTool(options={"working_directory": None})

    @pytest.mark.asyncio
    async def test_fetch_pr_success(self, tool, monkeypatch):
        """Test successful PR fetch with authentication."""
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_token_12345")  # pragma: allowlist secret
        _urls, captured_headers = _mock_aiohttp_get(
            {
                "head": {"sha": "abc123def456"},  # pragma: allowlist secret
                "state": "closed",
                "merged": True,
            }
        )

        result = await tool.run(
            input=GetGithubPullRequestToolInput(pr_url="https://GitHub.COM/torvalds/linux/pull/42")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data["head_sha"] == "abc123def456"  # pragma: allowlist secret
        assert data["state"] == "closed"
        assert data["merged"] is True

        # Verify API was called with authentication
        assert len(captured_headers) == 1
        assert "Authorization" in captured_headers[0]
        assert captured_headers[0]["Authorization"] == "Bearer test_token_12345"
        assert "repos/torvalds/linux/pulls/42" in _urls[0]

    @pytest.mark.asyncio
    async def test_fetch_pr_without_token(self, tool, monkeypatch):
        """Test PR fetch works without token (fallback to unauthenticated)."""
        monkeypatch.delenv("GITHUB_READONLY_TOKEN", raising=False)
        _urls, captured_headers = _mock_aiohttp_get(
            {
                "head": {"sha": "unauthenticated_sha"},
                "state": "open",
                "merged": False,
            }
        )

        result = await tool.run(
            input=GetGithubPullRequestToolInput(pr_url="https://github.com/owner/repo/pull/123")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data["head_sha"] == "unauthenticated_sha"

        # Verify no Authorization header
        assert len(captured_headers) == 1
        assert "Authorization" not in captured_headers[0]

    @pytest.mark.asyncio
    async def test_invalid_pr_url(self, tool):
        """Test that invalid PR URL raises ToolError."""
        with pytest.raises(ToolError, match="Invalid GitHub PR URL"):
            await tool.run(
                input=GetGithubPullRequestToolInput(pr_url="https://example.com/not-a-pr")
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_api_error(self, tool, monkeypatch):
        """Test that API errors are properly handled."""
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_token")  # pragma: allowlist secret
        _mock_aiohttp_get_error("404 Not Found")

        with pytest.raises(ToolError, match="Failed to fetch GitHub PR"):
            await tool.run(
                input=GetGithubPullRequestToolInput(pr_url="https://github.com/owner/repo/pull/999")
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_malformed_json_is_reported_as_tool_error(self, tool):
        _mock_aiohttp_get_malformed_json()

        with pytest.raises(ToolError, match="Failed to fetch GitHub PR"):
            await tool.run(
                input=GetGithubPullRequestToolInput(pr_url="https://github.com/owner/repo/pull/999")
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_pr_url_with_patch_suffix(self, tool, monkeypatch):
        """Test PR URL with .patch suffix."""
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_token")  # pragma: allowlist secret
        _mock_aiohttp_get(
            {
                "head": {"sha": "patched_sha"},
                "state": "closed",
                "merged": True,
            }
        )

        result = await tool.run(
            input=GetGithubPullRequestToolInput(pr_url="https://github.com/owner/repo/pull/99.patch")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert result.result["head_sha"] == "patched_sha"


# ---------------------------------------------------------------------------
# GetGithubCompareTool
# ---------------------------------------------------------------------------


class TestGetGithubCompareTool:
    @pytest.fixture
    def tool(self):
        return GetGithubCompareTool(options={"working_directory": None})

    @pytest.mark.asyncio
    async def test_fetch_compare_success(self, tool, monkeypatch):
        """Test successful compare fetch with authentication."""
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_compare_token")  # pragma: allowlist secret
        _urls, captured_headers = _mock_aiohttp_get(
            {
                "commits": [
                    {"sha": "aaa111"},
                    {"sha": "bbb222"},
                    {"sha": "ccc333"},
                ]
            }
        )

        result = await tool.run(
            input=GetGithubCompareToolInput(
                repo_url="https://GITHUB.com/owner/repo",
                base_ref="v1.0",
                target_ref="v2.0",
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data["commits"] == ["aaa111", "bbb222", "ccc333"]

        # Verify API was called with authentication
        assert len(captured_headers) == 1
        assert "Authorization" in captured_headers[0]
        assert captured_headers[0]["Authorization"] == "Bearer test_compare_token"
        assert "repos/owner/repo/compare/v1.0...v2.0" in _urls[0]

    @pytest.mark.asyncio
    async def test_fetch_compare_without_token(self, tool, monkeypatch):
        """Test compare fetch works without token."""
        monkeypatch.delenv("GITHUB_READONLY_TOKEN", raising=False)
        _urls, captured_headers = _mock_aiohttp_get({"commits": [{"sha": "single_commit"}]})

        result = await tool.run(
            input=GetGithubCompareToolInput(
                repo_url="https://github.com/owner/repo.git",
                base_ref="v1.0",
                target_ref="v1.1",
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert result.result["commits"] == ["single_commit"]

        # Verify no Authorization header
        assert len(captured_headers) == 1
        assert "Authorization" not in captured_headers[0]

    @pytest.mark.asyncio
    async def test_invalid_repo_url(self, tool):
        """Test that invalid repository URL raises ToolError."""
        with pytest.raises(ToolError, match="Invalid GitHub repository URL"):
            await tool.run(
                input=GetGithubCompareToolInput(
                    repo_url="https://example.com/not-github",
                    base_ref="v1",
                    target_ref="v2",
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_api_error(self, tool, monkeypatch):
        """Test that API errors are properly handled."""
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_token")  # pragma: allowlist secret
        _mock_aiohttp_get_error("timeout")

        with pytest.raises(ToolError, match="Failed to fetch GitHub compare"):
            await tool.run(
                input=GetGithubCompareToolInput(
                    repo_url="https://github.com/owner/repo",
                    base_ref="old",
                    target_ref="new",
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_malformed_json_is_reported_as_tool_error(self, tool):
        _mock_aiohttp_get_malformed_json()

        with pytest.raises(ToolError, match="Failed to fetch GitHub compare"):
            await tool.run(
                input=GetGithubCompareToolInput(
                    repo_url="https://github.com/owner/repo",
                    base_ref="old",
                    target_ref="new",
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_empty_commits(self, tool, monkeypatch):
        """Test compare with no commits."""
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_token")  # pragma: allowlist secret
        _mock_aiohttp_get({"commits": []})

        result = await tool.run(
            input=GetGithubCompareToolInput(
                repo_url="https://github.com/owner/repo",
                base_ref="v1.0",
                target_ref="v1.0",  # Same ref
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert result.result["commits"] == []

    @pytest.mark.asyncio
    async def test_filters_none_commits(self, tool, monkeypatch):
        """Test that None commits and commits without SHA are filtered out."""
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_token")  # pragma: allowlist secret
        _mock_aiohttp_get(
            {
                "commits": [
                    {"sha": "valid1"},
                    None,  # Should be filtered
                    {"sha": "valid2"},
                    {},  # No SHA, should be filtered
                    {"sha": "valid3"},
                ]
            }
        )

        result = await tool.run(
            input=GetGithubCompareToolInput(
                repo_url="https://github.com/owner/repo",
                base_ref="v1.0",
                target_ref="v2.0",
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert result.result["commits"] == ["valid1", "valid2", "valid3"]

    @pytest.mark.asyncio
    async def test_url_encoding(self, tool, monkeypatch):
        """Test that refs with special characters are properly URL-encoded."""
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "test_token")  # pragma: allowlist secret
        captured_urls, _ = _mock_aiohttp_get({"commits": []})

        await tool.run(
            input=GetGithubCompareToolInput(
                repo_url="https://github.com/owner/repo",
                base_ref="release/1.0",
                target_ref="release/2.0",
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        # Verify URL encoding of forward slashes
        assert "release%2F1.0...release%2F2.0" in captured_urls[0]
