"""Tools for working with upstream repositories and fix URLs."""

import re
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, StringToolOutput, Tool, ToolError, ToolRunOptions

from common.validators import AbsolutePath
from utils import run_subprocess


class ExtractUpstreamRepositoryInput(BaseModel):
    upstream_fix_url: str = Field(description="URL to the upstream fix/commit")


class UpstreamRepository(BaseModel):
    """Represents an upstream git repository and commit information."""
    repo_url: str = Field(description="Git clone URL of the upstream repository")
    commit_hash: str = Field(description="Commit hash to cherry-pick")
    original_url: str = Field(description="Original upstream fix URL")
    pr_number: str | None = Field(default=None, description="Pull request or merge request number if this is a PR/MR URL, None otherwise")
    is_pr: bool = Field(default=False, description="True if this is a pull request or merge request URL")


class ExtractUpstreamRepositoryOutput(JSONToolOutput[UpstreamRepository]):
    pass


class ExtractUpstreamRepositoryTool(Tool[ExtractUpstreamRepositoryInput, ToolRunOptions, ExtractUpstreamRepositoryOutput]):
    name = "extract_upstream_repository"
    description = """
    Extract upstream repository URL and commit hash from a commit or pull request URL.

    Supports common formats:
    - GitHub/GitLab commit: https://domain.com/owner/repo/commit/hash or /-/commit/hash
    - GitHub/GitLab PR: https://domain.com/owner/repo/pull/123 or /merge_requests/123
    - Query param formats: ?id=hash or ?h=hash (for cgit/gitweb)

    For pull requests, fetches the head commit SHA from the PR.
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

            # Check if this is a pull request URL and extract owner/repo/PR number in one match
            pr_match = re.search(r'/([\w\-\.]+)/([\w\-\.]+)/pull/(\d+)(?:\.patch)?', parsed.path)
            mr_match = re.search(r'/([\w\-\.]+)/([\w\-\.]+)/-/merge_requests/(\d+)(?:\.patch)?', parsed.path)

            if pr_match or mr_match:
                # Handle GitHub Pull Request or GitLab Merge Request
                match = pr_match if pr_match else mr_match
                owner = match.group(1)
                repo = match.group(2)
                pr_number = match.group(3)

                # Fetch PR/MR information to get the head commit
                if pr_match:
                    # GitHub API
                    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
                else:
                    # GitLab API
                    api_url = f"https://{parsed.netloc}/api/v4/projects/{owner}%2F{repo}/merge_requests/{pr_number}"

                headers = {
                    'Accept': 'application/json',
                    'User-Agent': 'RHEL-Backport-Agent'
                }

                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                            response.raise_for_status()
                            data = await response.json()

                            # Extract commit hash from API response
                            if pr_match:
                                # GitHub: get head.sha
                                commit_hash = data['head']['sha']
                            else:
                                # GitLab: get sha
                                commit_hash = data['sha']

                except (aiohttp.ClientError, KeyError) as e:
                    raise ToolError(
                        f"Failed to fetch PR/MR information from {api_url}. "
                        f"The PR/MR might be private, deleted, or the API is unavailable. Error: {e}"
                    )

                # Construct repository URL
                repo_url = f"https://{parsed.netloc}/{owner}/{repo}.git"

                # Return with PR information
                return ExtractUpstreamRepositoryOutput(
                    result=UpstreamRepository(
                        repo_url=repo_url,
                        commit_hash=commit_hash,
                        original_url=tool_input.upstream_fix_url,
                        pr_number=pr_number,
                        is_pr=True
                    )
                )

            else:
                # Handle regular commit URLs
                commit_hash = None
                repo_path = None

                # Pattern 1: /commit/hash or /-/commit/hash in the path (capture repo path and commit hash together)
                commit_match = re.search(r'^(.*?)(?:/(?:-/)?commit(?:s)?/([a-f0-9]{7,40})(?:\.patch)?)', parsed.path)
                if commit_match:
                    repo_path = commit_match.group(1).strip('/')
                    commit_hash = commit_match.group(2)

                # Pattern 2: query parameters (?id=hash or &h=hash for cgit/gitweb, ?p=repo for repo path)
                if not commit_hash and parsed.query:
                    query_match = re.search(r'(?:id|h)=([a-f0-9]{7,40})', parsed.query)
                    if query_match:
                        commit_hash = query_match.group(1)
                        # Extract repo from ?p= parameter
                        repo_query_match = re.search(r'[?&]p=([^;&]+)', parsed.query)
                        if repo_query_match:
                            repo_path = repo_query_match.group(1)

                if not commit_hash:
                    raise ToolError(f"Could not extract commit hash from URL: {tool_input.upstream_fix_url}")

                if not repo_path:
                    raise ToolError(f"Could not extract repository path from URL: {tool_input.upstream_fix_url}")

                # Construct clone URL
                scheme = parsed.scheme or 'https'
                repo_url = f"{scheme}://{parsed.netloc}/{repo_path}"
                if not repo_url.endswith('.git'):
                    repo_url += '.git'

            # Return for non-PR commits
            return ExtractUpstreamRepositoryOutput(
                result=UpstreamRepository(
                    repo_url=repo_url,
                    commit_hash=commit_hash,
                    original_url=tool_input.upstream_fix_url,
                    pr_number=None,
                    is_pr=False
                )
            )

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Error parsing upstream fix URL: {e}") from e


class CloneUpstreamRepositoryToolInput(BaseModel):
    repo_url: str = Field(description="Git clone URL of the upstream repository")
    clone_directory: AbsolutePath = Field(description="Absolute directory path where to clone the repository")


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
            clone_path = tool_input.clone_directory.parent / f"{tool_input.clone_directory.name}-upstream"

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
    repo_path: AbsolutePath = Field(description="Absolute path to the cloned upstream repository")
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
    If no matching tag is found, it returns an error.
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
            # Verify it's a git repository
            if not (tool_input.repo_path / ".git").exists():
                raise ToolError(f"Not a git repository: {tool_input.repo_path}")

            # Fetch all tags to ensure we have the latest
            cmd = ["git", "fetch", "--tags"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)
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
                exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

                if exit_code == 0:
                    found_tag = tag
                    break

            if not found_tag:
                # Get list of available tags for debugging
                cmd = ["git", "tag", "-l"]
                exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

                available_tags = stdout.strip().split('\n') if stdout.strip() else []
                tag_info = f"Available tags: {', '.join(available_tags[:10])}" if available_tags else "No tags found in repository"
                if len(available_tags) > 10:
                    tag_info += f" (and {len(available_tags) - 10} more)"

                raise ToolError(
                    f"Could not find tag matching version {tool_input.version}. "
                    f"Tried patterns: {', '.join(tag_patterns)}. "
                    f"{tag_info}. "
                )

            # Checkout the found tag
            cmd = ["git", "checkout", found_tag]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

            if exit_code != 0:
                raise ToolError(f"Failed to checkout tag {found_tag}: {stderr}")

            # Get the commit hash
            cmd = ["git", "rev-parse", "HEAD"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

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


class ApplyDownstreamPatchesToolInput(BaseModel):
    repo_path: AbsolutePath = Field(description="Absolute path to the upstream repository where patches will be applied")
    patch_files: list[str] = Field(description="List of patch filenames to apply in order")
    patches_directory: AbsolutePath = Field(description="Absolute directory path containing the patch files (usually the dist-git clone)")


class ApplyDownstreamPatchesTool(Tool[ApplyDownstreamPatchesToolInput, ToolRunOptions, StringToolOutput]):
    name = "apply_downstream_patches"
    description = """
    Apply existing patches from the dist-git spec file to the upstream repository.

    This recreates the current package state in the upstream repository by applying
    all the patches that are already part of the package. After this, we can cherry-pick
    the new fix on top.

    The patches are applied in order using 'git apply' and committed. If a patch fails to apply,
    the tool returns an error indicating which patch failed.
    """
    input_schema = ApplyDownstreamPatchesToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "upstream", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: ApplyDownstreamPatchesToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            # Verify it's a git repository
            if not (tool_input.repo_path / ".git").exists():
                raise ToolError(f"Not a git repository: {tool_input.repo_path}")

            # Verify patches directory exists
            if not tool_input.patches_directory.exists():
                raise ToolError(f"Patches directory does not exist: {tool_input.patches_directory}")

            if not tool_input.patch_files:
                return StringToolOutput(
                    result="No patches to apply (patch list is empty)"
                )

            applied_patches = []

            # Apply each patch in order
            for patch_file in tool_input.patch_files:
                patch_path = tool_input.patches_directory / patch_file

                # Check if patch file exists
                if not patch_path.exists():
                    raise ToolError(
                        f"Patch file not found: {patch_path}. "
                        f"Successfully applied: {', '.join(applied_patches) if applied_patches else 'none'}. "
                        "Abort cherry-pick approach, use git am workflow."
                    )

                # Try to apply the patch with git apply and commit
                # Use git apply instead of git am because dist-git patches can be plain diffs, not mbox format
                cmd = ["git", "apply", str(patch_path)]
                exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

                if exit_code != 0:
                    raise ToolError(
                        f"Failed to apply existing patch '{patch_file}' to upstream base version. "
                        f"Git apply error: {stderr}. "
                        f"Successfully applied: {', '.join(applied_patches) if applied_patches else 'none'}. "
                        "Abort cherry-pick approach, use git am workflow."
                    )

                # Stage the changes
                cmd = ["git", "add", "-A"]
                exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

                if exit_code != 0:
                    raise ToolError(f"Failed to stage changes after applying {patch_file}: {stderr}")

                # Commit the patch
                cmd = ["git", "commit", "-m", f"Apply {patch_file}"]
                exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

                if exit_code != 0:
                    raise ToolError(f"Failed to commit patch {patch_file}: {stderr}")

                applied_patches.append(patch_file)

            return StringToolOutput(
                result=f"Successfully applied {len(applied_patches)} patches: {', '.join(applied_patches)}"
            )

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e


class CherryPickCommitToolInput(BaseModel):
    repo_path: AbsolutePath = Field(description="Absolute path to the upstream repository")
    commit_hash: str = Field(description="Commit hash to cherry-pick")


class CherryPickCommitTool(Tool[CherryPickCommitToolInput, ToolRunOptions, StringToolOutput]):
    name = "cherry_pick_commit"
    description = """
    Cherry-pick a specific commit in the upstream repository.

    This applies the new fix on top of the existing patches. If there are no conflicts,
    the commit is applied successfully. If there are conflicts, the tool returns an error
    with information about the conflicting files so the agent can resolve them.
    """
    input_schema = CherryPickCommitToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "upstream", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: CherryPickCommitToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            # Verify it's a git repository
            if not (tool_input.repo_path / ".git").exists():
                raise ToolError(f"Not a git repository: {tool_input.repo_path}")

            # Check if commit exists locally
            cmd = ["git", "cat-file", "-t", tool_input.commit_hash]
            exit_code_check, _, _ = await run_subprocess(cmd, cwd=tool_input.repo_path)

            # If commit doesn't exist, try to fetch it
            if exit_code_check != 0:
                # Try to fetch the specific commit from origin
                cmd = ["git", "fetch", "origin", tool_input.commit_hash]
                exit_code_fetch, stdout_fetch, stderr_fetch = await run_subprocess(cmd, cwd=tool_input.repo_path)

                # Check again if commit exists after fetch
                cmd = ["git", "cat-file", "-t", tool_input.commit_hash]
                exit_code_check, _, _ = await run_subprocess(cmd, cwd=tool_input.repo_path)

                if exit_code_check != 0:
                    raise ToolError(
                        f"Commit {tool_input.commit_hash} not found in repository even after fetch attempt. "
                        f"Fetch result: {stderr_fetch if exit_code_fetch != 0 else 'succeeded but commit still unavailable'}. "
                        "Abort cherry-pick approach, use git am workflow."
                    )

            # Try to cherry-pick the commit
            cmd = ["git", "cherry-pick", tool_input.commit_hash]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

            if exit_code == 0:
                # Success - no conflicts
                return StringToolOutput(
                    result=f"Successfully cherry-picked commit {tool_input.commit_hash}"
                )

            # Check if it's a conflict or other error
            cmd = ["git", "status", "--porcelain"]
            exit_code_status, stdout_status, stderr_status = await run_subprocess(cmd, cwd=tool_input.repo_path)

            if exit_code_status == 0 and stdout_status:
                # Get list of conflicting files
                conflict_files = []
                for line in stdout_status.strip().split('\n'):
                    if line.startswith('UU ') or line.startswith('AA ') or line.startswith('DD '):
                        # UU = both modified, AA = both added, DD = both deleted
                        conflict_files.append(line[3:].strip())

                if conflict_files:
                    return StringToolOutput(
                        result=f"Cherry-pick has conflicts in the following files: {', '.join(conflict_files)}. "
                        f"Resolve the conflicts manually, then use cherry_pick_continue tool. "
                        f"Git error: {stderr}"
                    )

            # Some other error
            raise ToolError(
                f"Cherry-pick failed with error: {stderr}. "
                f"This may indicate the commit doesn't exist or is not compatible. "
                "Abort cherry-pick approach, use git am workflow."
            )

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e


class CherryPickContinueToolInput(BaseModel):
    repo_path: AbsolutePath = Field(description="Absolute path to the upstream repository")


class CherryPickContinueTool(Tool[CherryPickContinueToolInput, ToolRunOptions, StringToolOutput]):
    name = "cherry_pick_continue"
    description = """
    Continue a cherry-pick operation after conflicts have been resolved.

    Before calling this tool, all conflicts must be resolved and changes staged with 'git add'.
    This tool will complete the cherry-pick and create the commit.
    """
    input_schema = CherryPickContinueToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "upstream", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: CherryPickContinueToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            # Verify it's a git repository
            if not (tool_input.repo_path / ".git").exists():
                raise ToolError(f"Not a git repository: {tool_input.repo_path}")
            # Log current git state for debugging
            cmd = ["git", "status", "--short"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)
            if exit_code == 0:
                print(f"DEBUG: Git status before cherry-pick continue: {stdout}")
            else:
                print(f"DEBUG: Failed to get git status: {stderr}")

            # Check if we're in a cherry-pick state
            cmd = ["git", "status", "--porcelain"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

            if exit_code != 0:
                raise ToolError(f"Failed to check git status: {stderr}")

            # Validate stdout is not None
            if stdout is None:
                raise ToolError("Git status command returned no output")

            # Check if we're actually in a cherry-pick state by looking for .git/CHERRY_PICK_HEAD
            if not (tool_input.repo_path / ".git" / "CHERRY_PICK_HEAD").exists():
                raise ToolError("Not in a cherry-pick state. Cannot continue cherry-pick.")

            # Check for unresolved conflicts by checking git status
            # Files with UU, AA, DD status indicate unresolved conflicts
            for line in (stdout or "").strip().split('\n') if (stdout or "").strip() else []:
                if line.startswith('UU ') or line.startswith('AA ') or line.startswith('DD '):
                    conflict_file = line[3:].strip()
                    raise ToolError(
                        f"Unresolved conflicts still exist in: {conflict_file}. "
                        "File is still in conflict state. Use `git add` after resolving."
                    )

            # If no UU/AA/DD files, conflicts are resolved
            # The agent should have edited files and staged them with `git add`

            # Stage all resolved files
            cmd = ["git", "add", "-A"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

            if exit_code != 0:
                raise ToolError(f"Failed to stage resolved files: {stderr}")

            # Continue the cherry-pick
            cmd = ["git", "cherry-pick", "--continue"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

            if exit_code != 0:
                raise ToolError(
                    f"Failed to continue cherry-pick: {stderr}. "
                    "Abort cherry-pick approach, use git am workflow."
                )

            return StringToolOutput(
                result="Successfully completed cherry-pick after resolving conflicts"
            )

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e


class GeneratePatchFromCommitToolInput(BaseModel):
    repo_path: AbsolutePath = Field(description="Absolute path to the upstream repository")
    output_directory: AbsolutePath = Field(description="Absolute directory path where to save the generated patch file")
    patch_filename: str = Field(description="Name for the generated patch file (e.g., 'fix-cve-2024-1234.patch')")
    base_commit: str | None = Field(default=None, description="Base commit hash to generate patch from (generates patch for base_commit..HEAD). If not provided, generates patch for only HEAD commit.")


class GeneratePatchFromCommitTool(Tool[GeneratePatchFromCommitToolInput, ToolRunOptions, StringToolOutput]):
    name = "generate_patch_from_commit"
    description = """
    Generate a patch file from cherry-picked commits.

    This uses 'git format-patch' to create a proper patch file.
    - If base_commit is provided: generates ONE patch file containing all commits from base_commit..HEAD
    - If base_commit is not provided: generates a patch for only the last commit (HEAD)

    The patch will include commit messages and all changes.
    """
    input_schema = GeneratePatchFromCommitToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "upstream", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: GeneratePatchFromCommitToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            # Verify it's a git repository
            if not (tool_input.repo_path / ".git").exists():
                raise ToolError(f"Not a git repository: {tool_input.repo_path}")

            # Verify output directory exists
            if not tool_input.output_directory.exists():
                raise ToolError(f"Output directory does not exist: {tool_input.output_directory}")

            # Check if there are any commits to generate patch from
            cmd = ["git", "log", "--oneline", "-1"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)
            if exit_code != 0 or not stdout:
                raise ToolError("No commits found to generate patch from")

            # Generate patch using git format-patch
            # If base_commit is provided, generate patch for all commits from base_commit..HEAD
            # Otherwise, generate patch for only HEAD commit (-1)
            if tool_input.base_commit:
                cmd = ["git", "format-patch", f"{tool_input.base_commit}..HEAD", "--stdout"]
            else:
                cmd = ["git", "format-patch", "-1", "HEAD", "--stdout"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repo_path)

            if exit_code != 0:
                raise ToolError(f"Failed to generate patch: {stderr}")

            if not stdout:
                raise ToolError("Generated patch is empty")

            # Write the patch to the specified file
            patch_path = tool_input.output_directory / tool_input.patch_filename

            # Check if file already exists
            if patch_path.exists():
                raise ToolError(f"Patch file already exists: {patch_path}")

            with open(patch_path, 'w') as f:
                f.write(stdout)

            return StringToolOutput(
                result=f"Successfully generated patch file: {patch_path.absolute()}"
            )

        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e
