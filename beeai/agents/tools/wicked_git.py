from pathlib import Path

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions

from common.validators import AbsolutePath
from utils import run_subprocess


class GitPreparePackageSourcesInput(BaseModel):
    unpacked_sources_path: AbsolutePath = Field(
        description="Absolute path to the unpacked sources which result from `centpkg prep`",
    )


class GitPreparePackageSources(Tool[GitPreparePackageSourcesInput, ToolRunOptions, StringToolOutput]):
    name = "git_prepare_package_sources"
    description = """
    Prepares the package sources for application of the upstream fix.
    """
    input_schema = GitPreparePackageSourcesInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "git", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: GitPreparePackageSourcesInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            tool_input_path = tool_input.unpacked_sources_path
            if not tool_input_path.exists():
                return StringToolOutput(result=f"ERROR: provided path does not exist: {tool_input_path}")
            if not (tool_input_path / ".git").exists():
                # let's create it and initialize it
                cmd = ["git", "init"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    return StringToolOutput(result=f"ERROR: git init failed: {stderr}")
                cmd = ["git", "add", "-A"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    return StringToolOutput(result=f"ERROR: git add failed: {stderr}")
                cmd = ["git", "commit", "-m", "Initial commit"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    return StringToolOutput(result=f"ERROR: git commit failed: {stderr}")
            # commit changes if the repo is dirty
            cmd = ["git", "status", "--porcelain"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if exit_code != 0:
                return StringToolOutput(result=f"ERROR: git status failed: {stderr}")
            if stdout:
                cmd = ["git", "add", "-A"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    return StringToolOutput(result=f"ERROR: git add failed: {stderr}")
                cmd = ["git", "commit", "-m", "Apply %prep changes"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    return StringToolOutput(result=f"ERROR: git commit failed: {stderr}")
            return StringToolOutput(
                result=f"Successfully prepared the package sources at {tool_input_path}"
                        " for application of the upstream fix")
        except Exception as e:
            # we absolutely need to do this otherwise the error won't appear anywhere
            return StringToolOutput(result=f"ERROR: {e}")


class GitPatchCreationToolInput(BaseModel):
    repository_path: AbsolutePath = Field(description="Absolute path to the git repository")
    patch_file_path: AbsolutePath = Field(description="Absolute path where the patch file should be saved")


class GitPatchCreationTool(Tool[GitPatchCreationToolInput, ToolRunOptions, StringToolOutput]):
    name = "git_patch_create"
    description = """
    Creates a patch file from the specified git repository with an active git-am session.
    The tool expects you resolved all conflicts. It generates a patch file that can be
    applied later in the RPM build process.
    """
    input_schema = GitPatchCreationToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "git", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: GitPatchCreationToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        try:
            # Ensure the repository path exists and is a git repository
            tool_input_path = tool_input.repository_path
            if not tool_input_path.exists():
                return StringToolOutput(result=f"ERROR: Repository path does not exist: {tool_input_path}")

            git_dir = tool_input_path / ".git"
            if not git_dir.exists():
                return StringToolOutput(result=f"ERROR: Not a git repository: {tool_input_path}")

            cmd = ["git", "status"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if "am session" not in stdout:
                # am session is not active, we can reuse the patch file
                return StringToolOutput(result=f"The patch applied cleanly, you can use the patch file as is.")

            # list all untracked files in the repository
            rej_candidates = []
            cmd = ["git", "ls-files", "--others", "--exclude-standard"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if exit_code != 0:
                return StringToolOutput(result=f"ERROR: Git command failed: {stderr}")
            if stdout:  # none means no untracked files
                rej_candidates.extend(stdout.splitlines())
            # list staged as well since that's what the agent usually does after it resolves conflicts
            cmd = ["git", "diff", "--name-only", "--cached"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if exit_code != 0:
                return StringToolOutput(result=f"ERROR: Git command failed: {stderr}")
            if stdout:
                rej_candidates.extend(stdout.splitlines())
            if rej_candidates:
                # make sure there are no *.rej files in the repository
                rej_files = [file for file in rej_candidates if file.endswith(".rej")]
                if rej_files:
                    return StringToolOutput(result=f"ERROR: Merge conflicts detected in the repository: "
                                            f"{tool_input.repository_path}, {rej_files}")

            # git-am leaves the repository in a dirty state, so we need to stage everything
            # I considered to inspect the patch and only stage the files that are changed by the patch,
            # but the backport process could create new files or change new ones
            # so let's go the naive route: git add -A
            cmd = ["git", "add", "-A"]
            exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if exit_code != 0:
                return StringToolOutput(result=f"ERROR: Git command failed: {stderr}")
            # continue git-am process
            cmd = ["git", "am", "--continue"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if exit_code != 0:
                # if the patch file doesn't have the header, this will fail
                # let's verify in the error message
                if "fatal: empty ident name " in stderr:
                    exit_code, stdout, stderr = await run_subprocess(
                        ["git", "commit", "-m", f"Patch {tool_input.patch_file_path.name}"], cwd=tool_input_path)
                    if exit_code != 0:
                        raise ToolError(f"Command git-commit failed: {stderr}")
                    exit_code, stdout, stderr = await run_subprocess(
                        ["git", "am", "--skip"], cwd=tool_input_path)
                    if exit_code != 0:
                        raise ToolError(f"Command git-am failed: {stderr}")
                else:
                    raise ToolError(f"Command git-am failed: {stderr} out={stdout}")
            # good, now we should have the patch committed, so let's get the file
            cmd = [
                "git", "format-patch",
                "--output",
                str(tool_input.patch_file_path),
                "HEAD~1..HEAD"
            ]
            exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if exit_code != 0:
                raise ToolError(f"Command git-format-patch failed: {stderr}")
            return StringToolOutput(result=f"Successfully created a patch file: {tool_input.patch_file_path}")
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}")


class GitLogSearchToolInput(BaseModel):
    repository_path: AbsolutePath = Field(description="Absolute path to the git repository")
    cve_id: str = Field(description="CVE ID to look for in git history")
    jira_issue: str = Field(description="Jira issue to look for in git history")


class GitLogSearchTool(Tool[GitLogSearchToolInput, ToolRunOptions, StringToolOutput]):
    name = "git_log_search"
    description = """
    Searches the git history for a reference to either the provided cve_id or jira_issue.
    Returns the commit hash and the commit message.
    """
    input_schema = GitLogSearchToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "git", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: GitLogSearchToolInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        repo_path = tool_input.repository_path
        if not repo_path.exists():
            return StringToolOutput(result=f"ERROR: Repository path does not exist: {repo_path}")

        if not (repo_path / ".git").exists():
            return StringToolOutput(result=f"ERROR: Not a git repository: {repo_path}")
        search = tool_input.cve_id or tool_input.jira_issue
        if not search:
            return StringToolOutput(
                result="ERROR: No search string provided, jira_issue or cve_id is required")

        cmd = [
            "git",
            "log",
            "--no-merges",
            f"--grep={search}",
            "-n", "1",
            f"--pretty=%s %H",
        ]

        exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repo_path)
        if exit_code != 0:
            return StringToolOutput(result=f"ERROR: Git command failed: {stderr}")

        output = (stdout or "").strip()
        if not output:
            return StringToolOutput(result=f"No matches found for '{search}'")

        lines = output.splitlines()
        header = f"Found {len(lines)} matching commit(s) for '{search}'"
        # We do not return the output because it could confuse the agent
        return StringToolOutput(result=header)
