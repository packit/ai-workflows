import subprocess
from contextlib import asynccontextmanager

import aiohttp
import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError
from flexmock import flexmock

import ymir.tools.unprivileged.upstream_tools as upstream_tools_mod
from ymir.tools.unprivileged.upstream_tools import (
    ApplyDownstreamPatchesTool,
    ApplyDownstreamPatchesToolInput,
    CherryPickCommitTool,
    CherryPickCommitToolInput,
    CherryPickContinueTool,
    CherryPickContinueToolInput,
    CloneUpstreamRepositoryTool,
    CloneUpstreamRepositoryToolInput,
    ExtractUpstreamRepositoryInput,
    ExtractUpstreamRepositoryTool,
    FindBaseCommitTool,
    FindBaseCommitToolInput,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_aiohttp_get(json_data, status=200):
    """Mock aiohttp.ClientSession.get to return json_data.

    Returns captured_urls list for asserting which URLs were called.
    """
    captured_urls = []

    @asynccontextmanager
    async def fake_get(url, **kwargs):
        captured_urls.append(url)
        yield flexmock(
            json=lambda: _async_return(json_data),
            raise_for_status=lambda: None,
            status=status,
        )

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(fake_get)
    return captured_urls


def _mock_aiohttp_get_error(error_msg="error"):
    """Mock aiohttp.ClientSession.get to raise aiohttp.ClientError."""

    @asynccontextmanager
    async def fake_get(url, **kwargs):
        raise aiohttp.ClientError(error_msg)
        yield

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(fake_get)


async def _async_return(value):
    return value


# ---------------------------------------------------------------------------
# ExtractUpstreamRepositoryTool - URL parsing tests
# ---------------------------------------------------------------------------


class TestExtractUpstreamRepositoryTool:
    @pytest.fixture
    def tool(self):
        return ExtractUpstreamRepositoryTool(options={"working_directory": None})

    @pytest.mark.asyncio
    async def test_github_commit_url(self, tool):
        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://github.com/libexpat/libexpat/commit/a93ef2756c88c4e3e6e7e8a9f42daa06e90e8e5b"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://github.com/libexpat/libexpat.git"
        assert data.commit_hash == "a93ef2756c88c4e3e6e7e8a9f42daa06e90e8e5b"  # pragma: allowlist secret
        assert data.is_pr is False
        assert data.is_compare is False

    @pytest.mark.asyncio
    async def test_github_commit_url_with_patch_suffix(self, tool):
        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://github.com/libexpat/libexpat/commit/abc1234.patch"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://github.com/libexpat/libexpat.git"
        assert data.commit_hash == "abc1234"

    @pytest.mark.asyncio
    async def test_gitlab_commit_url(self, tool):
        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://gitlab.com/owner/repo/-/commit/deadbeef1234567"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://gitlab.com/owner/repo.git"
        assert data.commit_hash == "deadbeef1234567"  # pragma: allowlist secret
        assert data.is_pr is False

    @pytest.mark.asyncio
    async def test_cgit_query_param_url(self, tool):
        """Test gitweb-style URL with p= and h= query params (p= not first)."""
        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://git.example.org/gitweb?a=commitdiff&p=project.git&h=abcdef1234567890"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://git.example.org/project.git"
        assert data.commit_hash == "abcdef1234567890"  # pragma: allowlist secret

    @pytest.mark.asyncio
    async def test_kernel_org_cgit_url(self, tool):
        """kernel.org cgit URL: repo path in URL path, commit hash in ?id= query param."""
        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=abcdef1234567"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.commit_hash == "abcdef1234567"  # pragma: allowlist secret
        assert data.repo_url == "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"

    @pytest.mark.asyncio
    async def test_cgit_commit_path_url(self, tool):
        """Test cgit URL with commit hash embedded in the path."""
        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://git.savannah.gnu.org/cgit/grep.git/commit/abcdef1234567"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.commit_hash == "abcdef1234567"  # pragma: allowlist secret
        assert "grep.git" in data.repo_url

    @pytest.mark.asyncio
    async def test_github_pr_url(self, tool):
        _mock_aiohttp_get({"head": {"sha": "pr_commit_sha_1234567890abcdef"}})  # pragma: allowlist secret

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(upstream_fix_url="https://github.com/torvalds/linux/pull/42")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://github.com/torvalds/linux.git"
        assert data.commit_hash == "pr_commit_sha_1234567890abcdef"  # pragma: allowlist secret
        assert data.is_pr is True
        assert data.pr_number == "42"

    @pytest.mark.asyncio
    async def test_github_pr_url_with_patch_suffix(self, tool):
        _mock_aiohttp_get({"head": {"sha": "abc123def456"}})  # pragma: allowlist secret

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://github.com/owner/repo/pull/99.patch"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.is_pr is True
        assert data.pr_number == "99"

    @pytest.mark.asyncio
    async def test_gitlab_mr_url(self, tool):
        _mock_aiohttp_get({"sha": "mr_commit_sha_abcdef"})

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://gitlab.com/owner/repo/-/merge_requests/15"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://gitlab.com/owner/repo.git"
        assert data.commit_hash == "mr_commit_sha_abcdef"
        assert data.is_pr is True
        assert data.pr_number == "15"

    @pytest.mark.asyncio
    async def test_github_compare_url(self, tool):
        _mock_aiohttp_get(
            {
                "commits": [
                    {"sha": "aaa111"},
                    {"sha": "bbb222"},
                    {"sha": "ccc333"},
                ]
            }
        )

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://github.com/owner/repo/compare/v3.7.0...v3.7.1"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.is_compare is True
        assert data.base_ref == "v3.7.0"
        assert data.target_ref == "v3.7.1"
        assert data.compare_commits == ["aaa111", "bbb222", "ccc333"]
        assert data.commit_hash == "ccc333"
        assert data.repo_url == "https://github.com/owner/repo.git"

    @pytest.mark.asyncio
    async def test_gitlab_compare_url(self, tool):
        _mock_aiohttp_get(
            {
                "commits": [
                    {"id": "newest"},
                    {"id": "oldest"},
                ]
            }
        )

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://gitlab.com/owner/repo/-/compare/v1.0...v1.1"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.is_compare is True
        # GitLab commits are reversed (newest first in API -> oldest first in output)
        assert data.compare_commits == ["oldest", "newest"]
        assert data.commit_hash == "newest"

    @pytest.mark.asyncio
    async def test_compare_url_api_failure_falls_back_to_target_ref(self, tool):
        """When API is unavailable, compare URL still returns target_ref as commit_hash."""
        _mock_aiohttp_get_error("timeout")

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://github.com/owner/repo/compare/v1.0...v1.1"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.is_compare is True
        assert data.commit_hash == "v1.1"
        assert data.compare_commits is None

    @pytest.mark.asyncio
    async def test_pr_api_failure_raises_tool_error(self, tool):
        _mock_aiohttp_get_error("404")

        with pytest.raises(ToolError, match="Failed to fetch PR/MR information"):
            await tool.run(
                input=ExtractUpstreamRepositoryInput(
                    upstream_fix_url="https://github.com/owner/repo/pull/999"
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_unparseable_url_raises_tool_error(self, tool):
        with pytest.raises(ToolError, match="Could not extract commit hash"):
            await tool.run(
                input=ExtractUpstreamRepositoryInput(upstream_fix_url="https://example.com/not-a-commit-url")
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_commit_url_without_repo_path_raises_tool_error(self, tool):
        """cgit URL with commit hash but no repo path should error."""
        with pytest.raises(ToolError, match="Could not extract"):
            await tool.run(
                input=ExtractUpstreamRepositoryInput(upstream_fix_url="https://example.org/?id=abcdef1234567")
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_double_dot_compare_separator(self, tool):
        """Compare URLs with '..' separator should also work."""
        _mock_aiohttp_get({"commits": [{"sha": "only1"}]})

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://github.com/owner/repo/compare/v1.0..v1.1"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.is_compare is True
        assert data.base_ref == "v1.0"
        assert data.target_ref == "v1.1"

    @pytest.mark.asyncio
    async def test_compare_url_target_ref_ending_in_patch_chars(self, tool):
        """Compare URL where target_ref ends in characters from the set '.patch'."""
        _mock_aiohttp_get_error("skip API")

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://github.com/owner/repo/compare/v1.0...some-branch-path"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.target_ref == "some-branch-path"

    @pytest.mark.asyncio
    async def test_gitlab_nested_path_mr_url(self, tool):
        """GitLab MR URL with deeply nested project path (more than owner/repo)."""
        captured_urls = _mock_aiohttp_get({"sha": "mr_head_commit"})

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://gitlab.com/redhat/centos-stream/rpms/bind/-/merge_requests/15"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://gitlab.com/redhat/centos-stream/rpms/bind.git"
        assert "redhat%2Fcentos-stream%2Frpms%2Fbind" in captured_urls[0]
        assert "/merge_requests/15" in captured_urls[0]

    @pytest.mark.asyncio
    async def test_gitlab_nested_path_compare_url(self, tool):
        """GitLab compare URL with deeply nested project path."""
        captured_urls = _mock_aiohttp_get({"commits": [{"id": "abc123"}]})

        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://gitlab.com/redhat/centos-stream/rpms/bind/-/compare/v9.18.0...v9.18.1"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://gitlab.com/redhat/centos-stream/rpms/bind.git"
        assert "redhat%2Fcentos-stream%2Frpms%2Fbind" in captured_urls[0]
        assert "/repository/compare" in captured_urls[0]

    @pytest.mark.asyncio
    async def test_cgit_p_param_at_start_of_query(self, tool):
        """cgit/gitweb URL where p= is the first query parameter."""
        result = await tool.run(
            input=ExtractUpstreamRepositoryInput(
                upstream_fix_url="https://git.example.org/gitweb?p=project.git&h=abcdef1234567"
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        data = result.result
        assert data.repo_url == "https://git.example.org/project.git"
        assert data.commit_hash == "abcdef1234567"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# CloneUpstreamRepositoryTool
# ---------------------------------------------------------------------------


class TestCloneUpstreamRepositoryTool:
    @pytest.fixture
    def tool(self):
        return CloneUpstreamRepositoryTool(options={"working_directory": None})

    @pytest.mark.asyncio
    async def test_clone_success(self, tool, tmp_path):
        clone_dir = tmp_path / "mypackage"
        expected_path = tmp_path / "mypackage-upstream"

        async def mock_run_subprocess(cmd, **kwargs):
            expected_path.mkdir(parents=True, exist_ok=True)
            (expected_path / ".git").mkdir(exist_ok=True)
            return (0, "", "")

        flexmock(upstream_tools_mod).should_receive("run_subprocess").replace_with(mock_run_subprocess).once()

        result = await tool.run(
            input=CloneUpstreamRepositoryToolInput(
                repo_url="https://github.com/owner/repo.git",
                clone_directory=str(clone_dir),
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "Successfully cloned" in result.result
        assert "mypackage-upstream" in result.result

    @pytest.mark.asyncio
    async def test_clone_directory_already_exists(self, tool, tmp_path):
        clone_dir = tmp_path / "mypackage"
        (tmp_path / "mypackage-upstream").mkdir()

        with pytest.raises(ToolError, match="Clone directory already exists"):
            await tool.run(
                input=CloneUpstreamRepositoryToolInput(
                    repo_url="https://github.com/owner/repo.git",
                    clone_directory=str(clone_dir),
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_clone_git_failure(self, tool, tmp_path):
        clone_dir = tmp_path / "mypackage"

        async def mock_run_subprocess(cmd, **kwargs):
            return (128, "", "fatal: repository not found")

        flexmock(upstream_tools_mod).should_receive("run_subprocess").replace_with(mock_run_subprocess)

        with pytest.raises(ToolError, match="Git clone failed"):
            await tool.run(
                input=CloneUpstreamRepositoryToolInput(
                    repo_url="https://github.com/nonexistent/repo.git",
                    clone_directory=str(clone_dir),
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))


# ---------------------------------------------------------------------------
# FindBaseCommitTool
# ---------------------------------------------------------------------------


class TestFindBaseCommitTool:
    @pytest.fixture
    def upstream_repo(self, tmp_path):
        repo = tmp_path / "upstream"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True)
        (repo / "file.c").write_text("int main() {}\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True)
        subprocess.run(["git", "tag", "v1.2.3"], cwd=repo, check=True)
        subprocess.run(["git", "tag", "release-2.0.0"], cwd=repo, check=True)
        return repo

    @staticmethod
    def _make_repo(tmp_path, dir_name, tags):
        """Create a git repo at tmp_path/dir_name with the given tags."""
        repo = tmp_path / dir_name
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True)
        (repo / "file.c").write_text("int main() {}\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True)
        for tag in tags:
            subprocess.run(["git", "tag", tag], cwd=repo, check=True)
        return repo

    @pytest.fixture
    def tool(self):
        return FindBaseCommitTool(options={"working_directory": None})

    @pytest.mark.asyncio
    async def test_finds_v_prefixed_tag(self, tool, upstream_repo):
        result = await tool.run(
            input=FindBaseCommitToolInput(
                repo_path=str(upstream_repo),
                version="1.2.3",
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "v1.2.3" in result.result
        assert "base_tag_commit" in tool.options

    @pytest.mark.asyncio
    async def test_finds_release_prefixed_tag(self, tool, upstream_repo):
        result = await tool.run(
            input=FindBaseCommitToolInput(
                repo_path=str(upstream_repo),
                version="2.0.0",
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "release-2.0.0" in result.result

    @pytest.mark.asyncio
    async def test_explicit_tag_override(self, tool, upstream_repo):
        result = await tool.run(
            input=FindBaseCommitToolInput(
                repo_path=str(upstream_repo),
                version="99.99.99",
                tag="v1.2.3",
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "v1.2.3" in result.result

    @pytest.mark.asyncio
    async def test_explicit_commit_override(self, tool, upstream_repo):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=upstream_repo, capture_output=True, text=True, check=True
        ).stdout.strip()

        result = await tool.run(
            input=FindBaseCommitToolInput(
                repo_path=str(upstream_repo),
                version="99.99.99",
                commit=head,
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert head in result.result
        assert tool.options["base_tag_commit"] == head

    @pytest.mark.asyncio
    async def test_no_matching_tag_returns_soft_failure(self, tool, upstream_repo):
        result = await tool.run(
            input=FindBaseCommitToolInput(
                repo_path=str(upstream_repo),
                version="99.99.99",
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "Could not find tag matching version 99.99.99" in result.result
        assert "v1.2.3" in result.result
        assert "Retry with" in result.result
        assert "base_tag_commit" not in tool.options

    @pytest.mark.asyncio
    async def test_finds_curl_style_tag(self, tool, tmp_path):
        repo = self._make_repo(tmp_path, "curl-upstream", ["curl-7_76_1"])
        result = await tool.run(
            input=FindBaseCommitToolInput(repo_path=str(repo), version="7.76.1")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "curl-7_76_1" in result.result
        assert "base_tag_commit" in tool.options

    @pytest.mark.asyncio
    async def test_finds_openssh_style_tag(self, tmp_path):
        tool = FindBaseCommitTool(options={"working_directory": None})
        repo = self._make_repo(tmp_path, "openssh-upstream", ["V_9_9_P1"])
        result = await tool.run(
            input=FindBaseCommitToolInput(repo_path=str(repo), version="9.9p1")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "V_9_9_P1" in result.result
        assert "base_tag_commit" in tool.options

    @pytest.mark.asyncio
    async def test_finds_postgresql_style_tag(self, tmp_path):
        tool = FindBaseCommitTool(options={"working_directory": None})
        repo = self._make_repo(tmp_path, "postgresql-upstream", ["REL_16_11"])
        result = await tool.run(
            input=FindBaseCommitToolInput(repo_path=str(repo), version="16.11")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "REL_16_11" in result.result
        assert "base_tag_commit" in tool.options

    @pytest.mark.asyncio
    async def test_finds_gnutls_style_tag(self, tmp_path):
        tool = FindBaseCommitTool(options={"working_directory": None})
        repo = self._make_repo(tmp_path, "gnutls-upstream", ["gnutls_3_6_2"])
        result = await tool.run(
            input=FindBaseCommitToolInput(repo_path=str(repo), version="3.6.2")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "gnutls_3_6_2" in result.result
        assert "base_tag_commit" in tool.options

    @pytest.mark.asyncio
    async def test_finds_pkgname_dotted_tag(self, tmp_path):
        tool = FindBaseCommitTool(options={"working_directory": None})
        repo = self._make_repo(tmp_path, "mylib-upstream", ["mylib-2.0.0"])
        result = await tool.run(
            input=FindBaseCommitToolInput(repo_path=str(repo), version="2.0.0")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "mylib-2.0.0" in result.result
        assert "base_tag_commit" in tool.options

    @pytest.mark.asyncio
    async def test_smart_tag_listing_shows_relevant_tags(self, tmp_path):
        tool = FindBaseCommitTool(options={"working_directory": None})
        tags = ["curl-7_76_1", "curl-8_0_0", "curl-8_1_0", "unrelated-tag"]
        repo = self._make_repo(tmp_path, "upstream", tags)
        result = await tool.run(
            input=FindBaseCommitToolInput(repo_path=str(repo), version="7.76.1")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "Tags matching version components" in result.result
        assert "curl-7_76_1" in result.result

    @pytest.mark.asyncio
    async def test_v_prefixed_tag_wins_over_pkgname_pattern(self, tmp_path):
        """Generic patterns are tried before package-name patterns."""
        tool = FindBaseCommitTool(options={"working_directory": None})
        repo = self._make_repo(tmp_path, "curl-upstream", ["v7.76.1", "curl-7_76_1"])
        result = await tool.run(
            input=FindBaseCommitToolInput(repo_path=str(repo), version="7.76.1")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "v7.76.1" in result.result

    @pytest.mark.asyncio
    async def test_no_pkg_name_without_upstream_suffix(self, tmp_path):
        """Repos without -upstream suffix skip package-name patterns."""
        tool = FindBaseCommitTool(options={"working_directory": None})
        repo = self._make_repo(tmp_path, "somerepo", ["somerepo-1_0_0"])
        result = await tool.run(
            input=FindBaseCommitToolInput(repo_path=str(repo), version="1.0.0")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "Could not find tag" in result.result
        assert "somerepo-1_0_0" in result.result

    @pytest.mark.asyncio
    async def test_not_a_git_repo(self, tool, tmp_path):
        with pytest.raises(ToolError, match="Not a git repository"):
            await tool.run(
                input=FindBaseCommitToolInput(
                    repo_path=str(tmp_path),
                    version="1.0.0",
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))


# ---------------------------------------------------------------------------
# ApplyDownstreamPatchesTool
# ---------------------------------------------------------------------------


class TestApplyDownstreamPatchesTool:
    @pytest.fixture
    def upstream_repo(self, tmp_path):
        repo = tmp_path / "upstream"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True)
        (repo / "main.c").write_text("int main() { return 0; }\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True)
        return repo

    @pytest.fixture
    def patches_dir(self, tmp_path):
        d = tmp_path / "patches"
        d.mkdir()
        (d / "fix-one.patch").write_text(
            "--- a/main.c\n+++ b/main.c\n@@ -1 +1,2 @@\n int main() { return 0; }\n+/* fix one */\n"
        )
        (d / "fix-two.patch").write_text(
            "--- a/main.c\n"
            "+++ b/main.c\n"
            "@@ -1,2 +1,3 @@\n"
            " int main() { return 0; }\n"
            " /* fix one */\n"
            "+/* fix two */\n"
        )
        return d

    @pytest.fixture
    def tool(self):
        return ApplyDownstreamPatchesTool(options={"working_directory": None})

    @pytest.mark.asyncio
    async def test_apply_patches_success(self, tool, upstream_repo, patches_dir):
        result = await tool.run(
            input=ApplyDownstreamPatchesToolInput(
                repo_path=str(upstream_repo),
                patch_files=["fix-one.patch", "fix-two.patch"],
                patches_directory=str(patches_dir),
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "Successfully applied 2 patches" in result.result
        assert "fix-one.patch" in result.result
        assert "fix-two.patch" in result.result
        assert "base_head_commit" in tool.options

        content = (upstream_repo / "main.c").read_text()
        assert "/* fix one */" in content
        assert "/* fix two */" in content

    @pytest.mark.asyncio
    async def test_apply_empty_patch_list(self, tool, upstream_repo, patches_dir):
        result = await tool.run(
            input=ApplyDownstreamPatchesToolInput(
                repo_path=str(upstream_repo),
                patch_files=[],
                patches_directory=str(patches_dir),
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "No patches to apply" in result.result
        assert "base_head_commit" in tool.options

    @pytest.mark.asyncio
    async def test_missing_patch_file_raises(self, tool, upstream_repo, patches_dir):
        with pytest.raises(ToolError, match="Patch file not found"):
            await tool.run(
                input=ApplyDownstreamPatchesToolInput(
                    repo_path=str(upstream_repo),
                    patch_files=["nonexistent.patch"],
                    patches_directory=str(patches_dir),
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_conflicting_patch_raises(self, tool, upstream_repo, patches_dir):
        (patches_dir / "bad.patch").write_text(
            "--- a/main.c\n"
            "+++ b/main.c\n"
            "@@ -1,3 +1,4 @@\n"
            " this context does not exist\n"
            " neither does this\n"
            " or this\n"
            "+added line\n"
        )

        with pytest.raises(ToolError, match=r"Failed to apply existing patch 'bad\.patch'"):
            await tool.run(
                input=ApplyDownstreamPatchesToolInput(
                    repo_path=str(upstream_repo),
                    patch_files=["bad.patch"],
                    patches_directory=str(patches_dir),
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_custom_strip_levels(self, tool, upstream_repo, patches_dir):
        (patches_dir / "strip2.patch").write_text(
            "--- a/subdir/main.c\n"
            "+++ b/subdir/main.c\n"
            "@@ -1 +1,2 @@\n"
            " int main() { return 0; }\n"
            "+/* strip 2 applied */\n"
        )

        result = await tool.run(
            input=ApplyDownstreamPatchesToolInput(
                repo_path=str(upstream_repo),
                patch_files=["strip2.patch"],
                patches_directory=str(patches_dir),
                patch_strip_levels={"strip2.patch": 2},
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "Successfully applied 1 patches" in result.result
        assert "/* strip 2 applied */" in (upstream_repo / "main.c").read_text()

    @pytest.mark.asyncio
    async def test_not_a_git_repo(self, tool, tmp_path, patches_dir):
        with pytest.raises(ToolError, match="Not a git repository"):
            await tool.run(
                input=ApplyDownstreamPatchesToolInput(
                    repo_path=str(tmp_path),
                    patch_files=["fix-one.patch"],
                    patches_directory=str(patches_dir),
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))


# ---------------------------------------------------------------------------
# CherryPickCommitTool
# ---------------------------------------------------------------------------


class TestCherryPickCommitTool:
    @pytest.fixture
    def repo_with_branch(self, tmp_path):
        """Create a repo with a diverged branch to test cherry-pick scenarios."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True)
        (repo / "file.c").write_text("line 1\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True)

        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        (repo / "file.c").write_text("line 1\nfeature line\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Add feature"], cwd=repo, check=True)
        feature_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

        subprocess.run(["git", "checkout", "-"], cwd=repo, check=True)
        (repo / "file.c").write_text("line 1\nmain line\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Main change"], cwd=repo, check=True)

        return repo, feature_commit

    @pytest.fixture
    def tool(self):
        return CherryPickCommitTool(options={"working_directory": None})

    @pytest.mark.asyncio
    async def test_cherry_pick_success_no_conflict(self, tool, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True)
        (repo / "a.txt").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True)

        subprocess.run(["git", "checkout", "-b", "other"], cwd=repo, check=True)
        (repo / "b.txt").write_text("new file\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Add b.txt"], cwd=repo, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

        subprocess.run(["git", "checkout", "-"], cwd=repo, check=True)

        result = await tool.run(
            input=CherryPickCommitToolInput(repo_path=str(repo), commit_hash=commit)
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "Successfully cherry-picked" in result.result
        assert (repo / "b.txt").exists()

    @pytest.mark.asyncio
    async def test_cherry_pick_with_conflict(self, tool, repo_with_branch):
        repo, feature_commit = repo_with_branch

        result = await tool.run(
            input=CherryPickCommitToolInput(repo_path=str(repo), commit_hash=feature_commit)
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

        assert "conflicts" in result.result.lower()
        assert "file.c" in result.result
        assert tool.options.get("fix_commit") == feature_commit

    @pytest.mark.asyncio
    async def test_cherry_pick_commit_not_found(self, tool, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True)
        (repo / "a.txt").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=repo, check=True)

        with pytest.raises(ToolError, match="not found"):
            await tool.run(
                input=CherryPickCommitToolInput(repo_path=str(repo), commit_hash="deadbeefdeadbeef")
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    @pytest.mark.asyncio
    async def test_not_a_git_repo(self, tool, tmp_path):
        with pytest.raises(ToolError, match="Not a git repository"):
            await tool.run(
                input=CherryPickCommitToolInput(repo_path=str(tmp_path), commit_hash="abc1234")
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))


# ---------------------------------------------------------------------------
# CherryPickContinueTool
# ---------------------------------------------------------------------------


class TestCherryPickContinueTool:
    @pytest.fixture
    def conflicted_repo(self, tmp_path, monkeypatch):
        """Set up a repo in a cherry-pick conflict state."""
        monkeypatch.setenv("GIT_EDITOR", "true")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.com")
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.com")

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True)
        (repo / "file.c").write_text("original\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True)

        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        (repo / "file.c").write_text("feature change\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Feature"], cwd=repo, check=True)
        feature_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

        subprocess.run(["git", "checkout", "-"], cwd=repo, check=True)
        (repo / "file.c").write_text("main change\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Main"], cwd=repo, check=True)

        # Start cherry-pick that will conflict
        subprocess.run(["git", "cherry-pick", feature_commit], cwd=repo)
        return repo

    @pytest.fixture
    def tool(self):
        return CherryPickContinueTool(options={"working_directory": None})

    @pytest.mark.asyncio
    async def test_continue_after_resolving_conflicts(self, tool, conflicted_repo):
        (conflicted_repo / "file.c").write_text("resolved content\n")
        subprocess.run(["git", "add", "file.c"], cwd=conflicted_repo, check=True)

        result = await tool.run(input=CherryPickContinueToolInput(repo_path=str(conflicted_repo))).middleware(
            GlobalTrajectoryMiddleware(pretty=True)
        )

        assert "Successfully completed cherry-pick" in result.result

    @pytest.mark.asyncio
    async def test_continue_with_unresolved_conflicts(self, tool, conflicted_repo):
        with pytest.raises(ToolError, match="Unresolved conflicts"):
            await tool.run(input=CherryPickContinueToolInput(repo_path=str(conflicted_repo))).middleware(
                GlobalTrajectoryMiddleware(pretty=True)
            )

    @pytest.mark.asyncio
    async def test_not_in_cherry_pick_state(self, tool, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True)
        (repo / "a.txt").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=repo, check=True)

        with pytest.raises(ToolError, match="Not in a cherry-pick state"):
            await tool.run(input=CherryPickContinueToolInput(repo_path=str(repo))).middleware(
                GlobalTrajectoryMiddleware(pretty=True)
            )

    @pytest.mark.asyncio
    async def test_not_a_git_repo(self, tool, tmp_path):
        with pytest.raises(ToolError, match="Not a git repository"):
            await tool.run(input=CherryPickContinueToolInput(repo_path=str(tmp_path))).middleware(
                GlobalTrajectoryMiddleware(pretty=True)
            )
