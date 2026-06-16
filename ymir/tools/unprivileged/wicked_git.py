import re
import shutil
from pathlib import Path

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.common.base_utils import is_cs_branch, run_subprocess
from ymir.common.validators import AbsolutePath
from ymir.common.version_utils import parse_branch_name
from ymir.tools.base import CloneableTool as Tool


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
        self,
        tool_input: GitPreparePackageSourcesInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        try:
            tool_input_path = tool_input.unpacked_sources_path
            if not tool_input_path.exists():
                raise ToolError(f"Provided path does not exist: {tool_input_path}")
            if not (tool_input_path / ".git").exists():
                # let's create it and initialize it
                cmd = ["git", "init"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    raise ToolError(f"Command git-init failed: {stderr}")
                cmd = ["git", "add", "-A"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    raise ToolError(f"Command git-add failed: {stderr}")
                cmd = ["git", "commit", "-m", "Initial commit"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    raise ToolError(f"Command git-commit failed: {stderr}")
            # commit changes if the repo is dirty
            cmd = ["git", "status", "--porcelain"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if exit_code != 0:
                raise ToolError(f"Command git-status failed: {stderr}")
            if stdout:
                cmd = ["git", "add", "-A"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    raise ToolError(f"Command git-add failed: {stderr}")
                cmd = ["git", "commit", "-m", "Apply %prep changes"]
                exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input_path)
                if exit_code != 0:
                    raise ToolError(f"Command git-commit failed: {stderr}")

            cmd = ["git", "rev-parse", "HEAD"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input_path)
            if exit_code != 0:
                raise ToolError(f"Command git-rev-parse failed: {stderr}")
            # we will use this commit as the base for the patch
            self.options["base_head_commit"] = stdout.strip()
            return StringToolOutput(
                result=f"Successfully prepared the package sources at {tool_input_path}"
                " for application of the upstream fix. "
                f"HEAD commit is: {self.options['base_head_commit']}"
            )
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e


def build_rpmdefines(dist_git_path: Path, branch: str, spec_path: Path) -> list[str]:
    if not (parsed := parse_branch_name(branch)):
        raise ToolError(f"Cannot parse branch name: {branch}")
    major, minor = parsed

    disttag = f"el{major}" if minor is None else f"el{major}_{minor}"
    root = str(dist_git_path)

    defines: list[str] = [
        "--define",
        f"_sourcedir {root}",
        "--define",
        f"_specdir {root}",
        "--define",
        f"_builddir {root}",
        "--define",
        f"_srcrpmdir {root}",
        "--define",
        f"_rpmdir {root}",
        "--define",
        f"dist .{disttag}",
        "--define",
        f"rhel {major}",
        "--eval",
        "%undefine fedora",
    ]

    if is_cs_branch(branch):
        defines.extend(
            [
                "--define",
                f"centos {major}",
            ]
        )

    if int(major) <= 10:
        try:
            content = spec_path.read_text(encoding="utf-8", errors="replace")
            for match in sorted({int(m) for m in re.findall(r"%{?patch(\d+)\b", content)}):
                defines.extend(
                    [
                        "--define",
                        f"patch{match}(-) %patch -P {match} %{{?**}}",
                    ]
                )
        except (OSError, UnicodeDecodeError):
            pass

    return defines


class RunPackagePrepInput(BaseModel):
    dist_git_path: AbsolutePath = Field(
        description="Absolute path to the cloned dist-git repository",
    )
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="Dist-git branch")


class RunPackagePrepTool(Tool[RunPackagePrepInput, ToolRunOptions, StringToolOutput]):
    name = "run_package_prep"
    description = """
    Runs the package prep step (source extraction + patch application) to verify
    that all patches apply cleanly. If prep fails, the build subdirectory is
    removed to prevent inspection of a partially-patched source tree.
    """
    input_schema = RunPackagePrepInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "package", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: RunPackagePrepInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        dist_git = tool_input.dist_git_path
        if not dist_git.exists():
            raise ToolError(f"Dist-git path does not exist: {dist_git}")

        spec_path = dist_git / f"{tool_input.package}.spec"
        defines = build_rpmdefines(dist_git, tool_input.dist_git_branch, spec_path)
        cmd = ["rpmbuild", *defines, "--nodeps", "-bp", str(spec_path)]

        exit_code, stdout, stderr = await run_subprocess(cmd, cwd=dist_git)

        if exit_code == 0:
            return StringToolOutput(result=f"Prep succeeded.\n{stdout}")

        # Prep failed — remove build subdirectories to prevent stale state.
        # rpmbuild creates directories like <package>-<version>/ under the dist-git root.
        for child in dist_git.iterdir():
            if child.is_dir() and child.name.startswith(tool_input.package + "-"):
                shutil.rmtree(child, ignore_errors=True)

        return StringToolOutput(
            result=f"Prep FAILED (exit code {exit_code}). "
            "Build directory has been cleaned up to prevent stale state.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )


class BuildSrpmInput(BaseModel):
    dist_git_path: AbsolutePath = Field(
        description="Absolute path to the cloned dist-git repository",
    )
    package: str = Field(description="Package name")
    dist_git_branch: str = Field(description="Dist-git branch")


class BuildSrpmTool(Tool[BuildSrpmInput, ToolRunOptions, StringToolOutput]):
    name = "build_srpm"
    description = """
    Builds a source RPM (SRPM) from the dist-git repository.
    Returns the absolute path to the generated SRPM file.
    """
    input_schema = BuildSrpmInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "package", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: BuildSrpmInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        dist_git = tool_input.dist_git_path
        if not dist_git.exists():
            raise ToolError(f"Dist-git path does not exist: {dist_git}")

        spec_path = dist_git / f"{tool_input.package}.spec"
        defines = build_rpmdefines(dist_git, tool_input.dist_git_branch, spec_path)
        cmd = ["rpmbuild", *defines, "--nodeps", "-bs", str(spec_path)]

        exit_code, stdout, stderr = await run_subprocess(cmd, cwd=dist_git)

        if exit_code != 0:
            return StringToolOutput(
                result=f"SRPM build FAILED (exit code {exit_code}).\nstdout: {stdout}\nstderr: {stderr}"
            )

        for line in stdout.splitlines():
            if line.startswith("Wrote:") and line.endswith(".src.rpm"):
                srpm_path = line.removeprefix("Wrote:").strip()
                return StringToolOutput(result=srpm_path)

        return StringToolOutput(
            result=f"SRPM build succeeded but could not find SRPM path in output.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )


def ensure_git_repository(repository_path: AbsolutePath) -> None:
    if not repository_path.exists():
        raise ToolError(f"Repository path does not exist: {repository_path}")
    if not (repository_path / ".git").exists():
        raise ToolError(f"Not a git repository: {repository_path}")


async def find_rej_files(repository_path: AbsolutePath) -> list[str]:
    # we want to be sure that a .gitignore rule wouldn't filter out `.rej` files
    cmd = ["git", "ls-files", "--others", "-X", "/dev/null", "--", "*.rej"]
    exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repository_path)
    if exit_code != 0:
        raise ToolError(f"Git command failed: {stderr}")
    return stdout.splitlines() if stdout else []


async def discover_patch_p(patch_file_path: AbsolutePath, repository_path: AbsolutePath) -> int:
    """
    Process the given patch file and figure out with which `-p` value the patch should be applied
    in the given repository.

    Using `git apply --stat` we parse the given patch and try to fit it into the given repository.
    """
    cmd = ["git", "apply", "--stat", str(patch_file_path)]
    exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repository_path)
    if exit_code != 0:
        # this means the patch is borked
        raise ToolError(f"Command git-apply --stat failed: {stderr}")
    # expat/lib/xmlparse.c                        |    8 -
    # .github/workflows/scripts/mass-cppcheck.sh  |    1
    # .github/workflows/data/exported-symbols.txt |    2
    # expat/lib/expat.h                           |   15 +
    lines = stdout.splitlines()
    files = [line.split("|")[0].strip() for line in lines if "|" in line]

    # git-apply hates -p0:
    #   "git diff header lacks filename information when removing 1 leading pathname component (line 5)"
    # but /usr/bin/patch should be able to handle -p0, so this is a TODO
    # Nikola checked Fedora spec files: 17 -p3, 10 -p4, 2 -p5
    for n in range(1, 6):
        split_this_many = n - 1
        for fi in files:
            stripped_fi = fi
            if split_this_many > 0:
                stripped_fi = fi.split("/", split_this_many)[-1]
            if (repository_path / stripped_fi).exists():
                # I know this is naive, but we certainly cannot check all files
                # because some may be missing in the checkout
                return n
    raise ToolError(f"Failed to discover the value for `-p` for patch file: {patch_file_path}")


class GitPatchCreationToolInput(BaseModel):
    repository_path: AbsolutePath = Field(description="Absolute path to the git repository")
    patch_file_path: AbsolutePath = Field(description="Absolute path where the patch file should be saved")


class GitPatchApplyToolInput(BaseModel):
    repository_path: AbsolutePath = Field(description="Absolute path to the git repository")
    patch_file_path: AbsolutePath = Field(description="Absolute path to the patch file to apply")


class GitPatchApplyTool(Tool[GitPatchApplyToolInput, ToolRunOptions, StringToolOutput]):
    name = "git_patch_apply"
    description = "Applies provided patch file to the specified git repository using git-am."
    input_schema = GitPatchApplyToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "git", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GitPatchApplyToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        ensure_git_repository(tool_input.repository_path)
        p = await discover_patch_p(tool_input.patch_file_path, tool_input.repository_path)
        try:
            cmd = ["git", "am", "--reject", f"-p{p}", str(tool_input.patch_file_path)]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repository_path)
            if exit_code != 0:
                reject_files = await find_rej_files(tool_input.repository_path)
                return StringToolOutput(
                    result="Patch application failed, please resolve the conflicts "
                    "and run the tool `git_apply_finish` after you resolved all the conflicts. "
                    "Output from git-am follows:\n"
                    f"stdout: {stdout}\n"
                    f"stderr: {stderr}\n"
                    f"Reject files: {reject_files}\n"
                )
            return StringToolOutput(result="Successfully applied the patch.")
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e


class GitPatchApplyFinishToolInput(BaseModel):
    repository_path: AbsolutePath = Field(description="Absolute path to the git repository")
    patch_file_path: AbsolutePath = Field(description="Absolute path to the patch file to apply")


class GitPatchApplyFinishTool(Tool[GitPatchApplyFinishToolInput, ToolRunOptions, StringToolOutput]):
    name = "git_apply_finish"
    description = """
    Before calling this tool, you must resolve all merge conflicts and delete all `*.rej` files.
    The tool continues a `git am` session that was paused due to conflicts.
    After continuing, it will stage all your changes and attempt to complete the patch application.
    If no changes are staged, the tool will skip the patch and continue.
    """
    input_schema = GitPatchApplyFinishToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "git", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GitPatchApplyFinishToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        ensure_git_repository(tool_input.repository_path)
        try:
            cmd = ["git", "status"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repository_path)
            if "am session" not in stdout:
                # am session is not active, we can reuse the patch file
                return StringToolOutput(result="The patch applied cleanly, you can use the patch file as is.")

            # list all untracked files in the repository
            rej_candidates = []
            cmd = ["git", "ls-files", "--others", "--exclude-standard"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repository_path)
            if exit_code != 0:
                raise ToolError(f"Git command failed: {stderr}")
            if stdout:  # none means no untracked files
                rej_candidates.extend(stdout.splitlines())
            # list staged as well since that's what the agent usually does after it resolves conflicts
            cmd = ["git", "diff", "--name-only", "--cached"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repository_path)
            if exit_code != 0:
                raise ToolError(f"Git command failed: {stderr}")
            if stdout:
                rej_candidates.extend(stdout.splitlines())
            if rej_candidates:
                # make sure there are no *.rej files in the repository
                rej_files = [file for file in rej_candidates if file.endswith(".rej")]
                if rej_files:
                    raise ToolError(
                        "Merge conflicts detected in the repository: "
                        f"{tool_input.repository_path}, {rej_files}"
                    )

            # git-am leaves the repository in a dirty state, so we need to stage everything
            # I considered to inspect the patch and only stage the files that are changed by the patch,
            # but the backport process could create new files or change new ones
            # so let's go the naive route: git add -A
            cmd = ["git", "add", "-A"]
            exit_code, _, stderr = await run_subprocess(cmd, cwd=tool_input.repository_path)
            if exit_code != 0:
                raise ToolError(f"Git command failed: {stderr}")
            # continue git-am process
            cmd = ["git", "am", "--reject", "--continue"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repository_path)
            if exit_code != 0:
                # if the patch file doesn't have the header, this will fail
                # let's verify in the error message
                if "fatal: empty ident name " in stderr:
                    exit_code, stdout, stderr = await run_subprocess(
                        [
                            "git",
                            "commit",
                            "-m",
                            f"Patch {tool_input.patch_file_path.name}",
                        ],
                        cwd=tool_input.repository_path,
                    )
                    if exit_code != 0:
                        raise ToolError(f"Command git-commit failed: {stderr}")
                    exit_code, stdout, stderr = await run_subprocess(
                        ["git", "am", "--reject", "--skip"],
                        cwd=tool_input.repository_path,
                    )
                    if exit_code != 0:
                        raise ToolError(f"Command git-am failed: {stderr}")
                # FIXME: we need to find a more reliable way to detect this
                # elif "error: Failed to merge in the changes" in stderr:
                elif "Patch failed at" in stdout:
                    reject_files = await find_rej_files(tool_input.repository_path)
                    return StringToolOutput(
                        result="`git am --continue` resulted in more merge conflicts. "
                        "Please resolve the conflicts and run the tool `git_apply_finish` again."
                        f"Output from git-am follows:\n"
                        f"stdout: {stdout}\n"
                        f"stderr: {stderr}\n"
                        f"Reject files: {reject_files}\n"
                    )
                elif "No changes - did you forget" in stdout:
                    exit_code, stdout, stderr = await run_subprocess(
                        ["git", "am", "--reject", "--skip"],
                        cwd=tool_input.repository_path,
                    )
                    if exit_code != 0:
                        reject_files = await find_rej_files(tool_input.repository_path)
                        return StringToolOutput(
                            result="No changes detected in the working tree nor in the staging area."
                            " The patch was skipped and we have more conflicts to resolve. "
                            f"Output from git-am follows:\n"
                            f"stdout: {stdout}\n"
                            f"stderr: {stderr}\n"
                            f"Reject files: {reject_files}\n"
                        )
                else:
                    raise ToolError(f"Command git-am failed: {stderr} out={stdout}")
            # good, now we should have the patch committed, so let's get the file
            return StringToolOutput(
                result="Successfully finished the patch application. "
                "You can use the tool `git_patch_create` to create the final patch file."
            )
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e


class GitPatchCreationTool(Tool[GitPatchCreationToolInput, ToolRunOptions, StringToolOutput]):
    name = "git_patch_create"
    description = """
    Creates a combined patch file from all commits made on top of the base HEAD commit.
    The base commit is determined automatically from a previous tool invocation
    (git_prepare_package_sources or apply_downstream_patches).

    The patch preserves individual commit messages (mbox format).
    If the output file already exists, it will be overwritten.
    """
    input_schema = GitPatchCreationToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "git", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: GitPatchCreationToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        ensure_git_repository(tool_input.repository_path)
        try:
            base_commit_sha = self.options.get("base_head_commit")
            if not base_commit_sha:
                raise ToolError(
                    "`base_head_commit` not found in options. "
                    "Ensure 'git_prepare_package_sources' "
                    "or 'apply_downstream_patches' is run before this tool. "
                    f"Options: {self.options}"
                )
            cmd = ["git", "format-patch", "--stdout", f"{base_commit_sha}..HEAD"]
            exit_code, stdout, stderr = await run_subprocess(cmd, cwd=tool_input.repository_path)
            if exit_code != 0:
                raise ToolError(f"Command git-format-patch failed: {stderr}")
            if not stdout:
                raise ToolError("Generated patch is empty")
            tool_input.patch_file_path.write_text(stdout, encoding="utf-8")
            return StringToolOutput(
                result=f"Successfully created a patch file: {tool_input.patch_file_path} "
                f"(base commit: {base_commit_sha})"
            )
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"ERROR: {e}") from e


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
        self,
        tool_input: GitLogSearchToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        repo_path = tool_input.repository_path
        if not repo_path.exists():
            raise ToolError(f"Repository path does not exist: {repo_path}")

        if not (repo_path / ".git").exists():
            raise ToolError(f"Not a git repository: {repo_path}")
        search = tool_input.cve_id or tool_input.jira_issue
        if not search:
            raise ToolError("No search string provided, jira_issue or cve_id is required")

        cmd = [
            "git",
            "log",
            "--no-merges",
            f"--grep={search}",
            "-n",
            "1",
            "--pretty=%s %H",
        ]

        exit_code, stdout, stderr = await run_subprocess(cmd, cwd=repo_path)
        if exit_code != 0:
            raise ToolError(f"Git command failed: {stderr}")

        output = (stdout or "").strip()
        if not output:
            return StringToolOutput(result=f"No matches found for '{search}'")

        lines = output.splitlines()
        header = f"Found {len(lines)} matching commit(s) for '{search}'"
        # We do not return the output because it could confuse the agent
        return StringToolOutput(result=header)
