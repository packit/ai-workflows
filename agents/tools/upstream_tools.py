"""Tools for working with upstream repositories and fix URLs."""

import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolError, ToolRunOptions


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
