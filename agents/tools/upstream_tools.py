"""Tools for working with upstream repositories and fix URLs."""

import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, StringToolOutput, Tool, ToolError, ToolRunOptions

from utils import run_subprocess


class ExtractUpstreamRepositoryInput(BaseModel):
    upstream_fix_url: str = Field(description="URL to the upstream fix/commit")


class UpstreamRepository(BaseModel):
    """Represents an upstream git repository and commit information."""
    repo_url: str = Field(description="Git clone URL of the upstream repository")
    commit_hash: str = Field(description="Commit hash to cherry-pick")
    original_url: str = Field(description="Original upstream fix URL")


class ExtractUpstreamRepositoryOutput(JSONToolOutput[UpstreamRepository]):
    pass


class ExtractUpstreamRepositoryTool(Tool[ExtractUpstreamRepositoryInput, ToolRunOptions, ExtractUpstreamRepositoryOutput]):
    name = "extract_upstream_repository"
    description = """
    Extract upstream repository URL and commit hash from a commit URL.
    
    Supports common formats:
    - GitHub/GitLab: https://domain.com/owner/repo/commit/hash or /-/commit/hash
    - Query param formats: ?id=hash or ?h=hash (for cgit/gitweb)
    
    Returns the git clone URL and commit hash needed for cherry-picking.
    """
    input_schema = ExtractUpstreamRepositoryInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "upstream", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: ExtractUpstreamRepositoryInput, options: ToolRunOptions | None, context: RunContext
    ) -> ExtractUpstreamRepositoryOutput:
        try:
            parsed = urlparse(tool_input.upstream_fix_url)
            
            # Try to find commit hash - first in path, then in query params
            commit_hash = None
            
            # Pattern 1: /commit/hash or /-/commit/hash in the path
            commit_match = re.search(r'/(?:-/)?commit(?:s)?/([a-f0-9]{7,40})(?:\.patch)?', parsed.path)
            if commit_match:
                commit_hash = commit_match.group(1)
            
            # Pattern 2: query parameters (?id=hash or &h=hash for cgit/gitweb)
            if not commit_hash and parsed.query:
                query_match = re.search(r'(?:id|h)=([a-f0-9]{7,40})', parsed.query)
                if query_match:
                    commit_hash = query_match.group(1)
            
            if not commit_hash:
                raise ToolError(f"Could not extract commit hash from URL: {tool_input.upstream_fix_url}")
            
            # Extract repository path (everything before /commit or /-/commit)
            repo_match = re.match(r'(.*?)(?:/(?:-/)?commit)', parsed.path)
            if not repo_match:
                # For query-based URLs, try to extract repo from ?p= parameter
                repo_query_match = re.search(r'[?&]p=([^;&]+)', parsed.query)
                if repo_query_match:
                    repo_path = repo_query_match.group(1)
                else:
                    raise ToolError(f"Could not extract repository path from URL: {tool_input.upstream_fix_url}")
            else:
                repo_path = repo_match.group(1).strip('/')
            
            # Construct clone URL
            scheme = parsed.scheme or 'https'
            repo_url = f"{scheme}://{parsed.netloc}/{repo_path}"
            if not repo_url.endswith('.git'):
                repo_url += '.git'
            
            return ExtractUpstreamRepositoryOutput(
                result=UpstreamRepository(
                    repo_url=repo_url,
                    commit_hash=commit_hash,
                    original_url=tool_input.upstream_fix_url
                )
            )
            
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Error parsing upstream fix URL: {e}") from e


class CloneUpstreamRepositoryToolInput(BaseModel):
    repo_url: str = Field(description="Git clone URL of the upstream repository")
    clone_directory: str = Field(description="Directory path where to clone the repository")


class CloneUpstreamRepositoryTool(Tool[CloneUpstreamRepositoryToolInput, ToolRunOptions, StringToolOutput]):
    name = "clone_upstream_repository"
    description = """
    Clone an upstream git repository to a specified directory.
    
    This is used to get a local copy of the upstream repository so we can:
    - Checkout a specific version/tag
    - Apply existing patches
    - Cherry-pick new fixes
    
    The directory will be created with '-upstream' suffix automatically to avoid conflicts.
    """
    input_schema = CloneUpstreamRepositoryToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "upstream", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: CloneUpstreamRepositoryToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            # Always append -upstream suffix to avoid conflicts with dist-git
            requested_path = Path(tool_input.clone_directory)
            clone_path = requested_path.parent / f"{requested_path.name}-upstream"
            
            # Check if directory already exists
            if clone_path.exists():
                raise ToolError(f"Clone directory already exists: {clone_path}")
            
            # Create parent directory if needed
            clone_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Clone the repository
            cmd = ["git", "clone", tool_input.repo_url, str(clone_path)]
            exit_code, stdout, stderr = await run_subprocess(cmd)
            
            if exit_code != 0:
                raise ToolError(f"Git clone failed: {stderr}")
            
            # Verify the clone was successful
            if not (clone_path / ".git").exists():
                raise ToolError(f"Clone completed but .git directory not found in {clone_path}")
            
            return StringToolOutput(
                result=f"Successfully cloned repository to {clone_path.absolute()}"
            )
            
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e


class FindBaseCommitToolInput(BaseModel):
    repo_path: str = Field(description="Path to the cloned upstream repository")
    version: str = Field(description="Version string to find (e.g., '2.5.3')")


class FindBaseCommitTool(Tool[FindBaseCommitToolInput, ToolRunOptions, StringToolOutput]):
    name = "find_base_commit"
    description = """
    Find and checkout a git tag matching the specified version in an upstream repository.
    
    This tool tries common tag naming patterns:
    - v{version} (e.g., v2.5.3)
    - {version} (e.g., 2.5.3)
    - release-{version} (e.g., release-2.5.3)
    - {version}-release (e.g., 2.5.3-release)
    
    If a matching tag is found, it checks out that tag and returns the commit hash.
    If no matching tag is found, it returns an error to trigger fallback to git am approach.
    """
    input_schema = FindBaseCommitToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "upstream", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: FindBaseCommitToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            repo_path = Path(tool_input.repo_path)
            
            # Verify it's a git repository
            if not (repo_path / ".git").exists():
                raise ToolError(f"Not a git repository: {repo_path}")
            
            # Fetch all tags to ensure we have the latest
            cmd = ["git", "fetch", "--tags"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repo_path)
            if exit_code != 0:
                # Non-fatal, continue anyway (might work with existing tags)
                pass
            
            # Common tag patterns to try
            tag_patterns = [
                f"v{tool_input.version}",
                f"{tool_input.version}",
                f"release-{tool_input.version}",
                f"{tool_input.version}-release",
                f"rel-{tool_input.version}",
                f"{tool_input.version}.0",  # Sometimes .0 is added
                f"v{tool_input.version}.0",
            ]
            
            found_tag = None
            
            # Try each pattern
            for tag in tag_patterns:
                # Check if tag exists
                cmd = ["git", "rev-parse", "--verify", f"refs/tags/{tag}"]
                exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repo_path)
                
                if exit_code == 0:
                    found_tag = tag
                    break
            
            if not found_tag:
                # Get list of available tags for debugging
                cmd = ["git", "tag", "-l"]
                exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repo_path)
                
                available_tags = stdout.strip().split('\n') if stdout.strip() else []
                tag_info = f"Available tags: {', '.join(available_tags[:10])}" if available_tags else "No tags found in repository"
                if len(available_tags) > 10:
                    tag_info += f" (and {len(available_tags) - 10} more)"
                
                raise ToolError(
                    f"Could not find tag matching version {tool_input.version}. "
                    f"Tried patterns: {', '.join(tag_patterns)}. "
                    f"{tag_info}. "
                    "Fallback to git am approach recommended."
                )
            
            # Checkout the found tag
            cmd = ["git", "checkout", found_tag]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repo_path)
            
            if exit_code != 0:
                raise ToolError(f"Failed to checkout tag {found_tag}: {stderr}")
            
            # Get the commit hash
            cmd = ["git", "rev-parse", "HEAD"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repo_path)
            
            if exit_code != 0:
                raise ToolError(f"Failed to get commit hash: {stderr}")
            
            commit_hash = stdout.strip()
            
            return StringToolOutput(
                result=f"Successfully checked out tag '{found_tag}' at commit {commit_hash}"
            )
            
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e
