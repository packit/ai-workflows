import asyncio
import copy
import logging
import os
import sys
import re
import traceback
from pathlib import Path
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.agents.requirement.prompts import RequirementAgentSystemPrompt
from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow

import tasks
from agents.build_agent import create_build_agent, get_prompt as get_build_prompt
from agents.log_agent import create_log_agent, get_prompt as get_log_prompt
from agents.package_update_steps import PackageUpdateStep, PackageUpdateState
from common.constants import JiraLabels, RedisQueues
from common.models import (
    BackportInputSchema,
    BackportOutputSchema,
    BuildInputSchema,
    BuildOutputSchema,
    LogInputSchema,
    LogOutputSchema,
    Task,
)
from common.utils import redis_client, fix_await
from constants import I_AM_JOTNAR, CAREFULLY_REVIEW_CHANGES
from observability import setup_observability
from tools.commands import RunShellCommandTool
from tools.specfile import GetPackageInfoTool
from tools.filesystem import GetCWDTool, RemoveTool
from tools.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    StrReplaceTool,
    ViewTool,
    SearchTextTool,
)
from tools.upstream_tools import (
    ApplyDownstreamPatchesTool,
    CherryPickCommitTool,
    CherryPickContinueTool,
    CloneUpstreamRepositoryTool,
    ExtractUpstreamRepositoryTool,
    FindBaseCommitTool,
    GeneratePatchFromCommitTool,
)
from tools.wicked_git import (
    GitLogSearchTool,
    GitPatchApplyTool,
    GitPatchApplyFinishTool,
    GitPatchCreationTool,
    GitPreparePackageSources,
)
from triage_agent import BackportData, ErrorData
from utils import (
    check_subprocess,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    mcp_tools,
    render_prompt,
)
from specfile import Specfile

logger = logging.getLogger(__name__)


def get_instructions() -> str:
    return """
      You are an expert on backporting upstream patches to packages in RHEL ecosystem.

      To backport upstream patches <UPSTREAM_PATCHES> to package <PACKAGE> in dist-git branch <DIST_GIT_BRANCH>, do the following:

      CRITICAL: Do NOT modify, delete, or touch any existing patches in the dist-git repository.
      Only add new patches for the current backport. Existing patches are there for a reason
      and must remain unchanged.

      1. Knowing Jira issue <JIRA_ISSUE>, CVE ID <CVE_ID> or both, use the `git_log_search` tool to check
         in the dist-git repository whether the issue/CVE has already been resolved. If it has,
         end the process with `success=True` and `status="Backport already applied"`.

      2. Use the `git_prepare_package_sources` tool to prepare package sources in directory <UNPACKED_SOURCES>
         for application of the upstream patch.

      3. Determine which backport approach to use:

         A. CHERRY-PICK WORKFLOW (Preferred - try this first):

            IMPORTANT: This workflow uses TWO separate git repositories:
            - <UNPACKED_SOURCES>: Git repository (from Step 2) containing unpacked and committed upstream sources
            - <UPSTREAM_REPO>: A temporary upstream repository clone (created in step 3c with -upstream suffix)

            When to use this workflow:
            - <UPSTREAM_PATCHES> is a list of commit or pull request URLs
            - This includes URLs with .patch suffix (e.g., https://github.com/.../commit/abc123.patch)
            - If URL extraction fails, fall back to approach B

            3a. Extract upstream repository information:
                - Use `extract_upstream_repository` tool with the upstream fix URL
                - This extracts the repository URL and commit hash
                - If extraction fails, fall back to approach B

            3b. Get package information from dist-git:
                - Use `get_package_info` tool with the spec file path from <UNPACKED_SOURCES>
                - This provides the package version and list of existing patch filenames

            3c. Clone the upstream repository to a SEPARATE directory:
                - Use `clone_upstream_repository` tool with:
                  * repository_url: from step 3a
                  * clone_directory: current working directory (the dist-git repository root)
                  * The tool automatically creates a directory with -upstream suffix as <UPSTREAM_REPO>
                - Steps 3d-3g work in <UPSTREAM_REPO>, NOT in <UNPACKED_SOURCES>

            3d. Find and checkout the base version in upstream:
                - Use `find_base_commit` tool with <UPSTREAM_REPO> path and package version from 3b
                - IMPORTANT: Save this base version commit hash using `run_shell_command`:
                  `git -C <UPSTREAM_REPO> rev-parse HEAD` - store this as UPSTREAM_BASE
                - If no matching tag found, try to find the base commit manually using `view` and `run_shell_command` tools
                - Look for any tags or commits that might correspond to the package version
                - Only fall back to approach B if you cannot find any reasonable base commit

            3e. Apply existing patches from dist-git to upstream:
                - Use `apply_downstream_patches` tool with:
                  * repo_path: <UPSTREAM_REPO> (where to apply)
                  * patches_directory: current working directory (dist-git root where patch files are located)
                  * patch_files: list from step 3b
                - This recreates the current package state in <UPSTREAM_REPO>
                - IMPORTANT: Save the current commit hash after applying patches using `run_shell_command`:
                  `git -C <UPSTREAM_REPO> rev-parse HEAD` - store this as PATCHED_BASE for patch generation
                - If any patch fails to apply, immediately fall back to approach B

            3f. Cherry-pick the fix in upstream:
                FOR PULL REQUESTS (if is_pr is True from step 3a):
                  * Download the PR patch to see all commits: `curl -L <original_url> -o /tmp/pr.patch`
                  * Parse the patch file to extract commit hashes (lines starting with "From ")
                    Each commit appears as "From <hash> Mon Sep DD ..." and has "[PATCH XX/YY]" in subject
                  * You now have the exact list of commits that are part of the PR
                  * Fetch PR branch: `git -C <UPSTREAM_REPO> fetch origin pull/<pr_number>/head:pr-branch`
                  * Cherry-pick each commit from the list, starting from the first (oldest)
                  * When conflicts occur (EXPECTED when backporting to older version):
                    - Understand what the commit is trying to do and why it conflicts
                    - Examine what's different between old and current version
                    - Identify if the commit depends on changes that aren't in the dist-git version:
                      * Missing helper functions, types, or macros
                      * API changes that happened between versions
                      * Structural changes to the codebase
                      * Test file reorganization (tests split/merged into different files)
                    - If prerequisites are missing, you have options:
                      * Cherry-pick the prerequisite commits first (from upstream history between dist-git version and PR)
                      * Or adapt the code to work without them (rewrite to use older APIs)
                      * Or manually backport just the needed helper functions
                    - For test file conflicts due to reorganization:
                      * NEVER SKIP TEST COMMITS - tests validate that your fix actually works!
                      * Check if test files exist in different locations in the old version
                      * Use git log in upstream repo to trace test file movements: `git -C <UPSTREAM_REPO> log --follow --all -- path/to/test_file`
                      * Merge test changes into existing test files that match the old structure
                      * Adapt test code to work with older test frameworks or patterns
                      * Don't skip tests just because file paths don't match - adapt them!
                      * For CVE fixes: tests often demonstrate the vulnerability - they're CRITICAL
                    - If adding NEW test files, ensure they're integrated into the build system:
                        check Makefile/CMakeLists.txt/meson.build and add to test lists if needed,
                        or verify they follow auto-discovery naming conventions (test_*.py, *_test.c)
                    - Intelligently adapt the changes to make them work with the older codebase
                  * Continue until all PR commits are successfully cherry-picked and adapted

                FOR SINGLE COMMITS (if is_pr is False):
                  * Use commit_hash from step 3a
                  * Cherry-pick this single commit

                CHERRY-PICKING PROCESS (ONE commit at a time - NEVER multiple at once):
                  1. Cherry-pick ONE commit: `cherry_pick_commit` tool with ONE commit hash
                  2. If conflicts occur (NORMAL for backporting):
                     a. View conflicting files to understand what's needed
                     b. Intelligently resolve by editing files with `str_replace`:
                        - Understand what the commit does
                        - Adapt to older codebase
                        - Add missing helpers if needed
                        - Rewrite to use older APIs if needed
                        - Prioritize preserving the patch's original logic. The final backport must still fix the original bug.
                     c. Stage ALL resolved files: `git -C <UPSTREAM_REPO> add <file>` for each file
                     d. Complete cherry-pick: `cherry_pick_continue` tool
                  3. CRITICAL: Only move to next commit after current one is FULLY COMPLETE
                  4. NEVER try to cherry-pick multiple commits at once
                  5. Do NOT fall back to approach B - keep cherry-picking through all PR commits
                  6. NEVER skip any commits - all commits must be adapted and cherry-picked

            3g. Generate the final patch file from upstream:
                - Use `generate_patch_from_commit` tool on <UPSTREAM_REPO>
                - Specify output_directory as current working directory (the dist-git repository root)
                - Use a descriptive name like <JIRA_ISSUE>.patch (e.g., if JIRA is RHEL-114639, use RHEL-114639.patch)
                - CRITICAL: Provide base_commit parameter with the PATCHED_BASE from step 3e
                  This ensures the patch includes ALL cherry-picked commits, not just the last one
                - IMPORTANT: Only create NEW patch files. Do NOT modify existing patches in the dist-git repository
                - This patch file is now ready to be added to the spec file

            3h. The cherry-pick workflow is complete! The generated patch file contains the cleanly
                cherry-picked fix. Continue with steps 4-6 below to add this patch to the spec file,
                verify it with `centpkg prep`, and build the SRPM.

                Note: You do NOT need to apply this patch to <UNPACKED_SOURCES>. The patch file
                will be automatically applied during the RPM build process when you run `centpkg prep`.

         B. GIT AM WORKFLOW (Fallback approach):

            Note: For this workflow, use the pre-downloaded patch files in the current working directory.
            They are called `<JIRA_ISSUE>-<N>.patch` where <N> is a 0-based index. For example,
            for a `RHEL-12345` Jira issue the first patch would be called `RHEL-12345-0.patch`.

            Backport all patches individually using the steps 3a and 3b below.

            3a. Backport one patch at a time using the following steps:
                - Use the `git_patch_apply` tool with the patch file: <JIRA_ISSUE>-<N>.patch
                - Resolve all conflicts and leave the repository in a dirty state. Delete all *.rej files.
                - Use the `git_apply_finish` tool to finish the patch application.

            3b. Once there are no more conflicts, use the `git_patch_create` tool with the patch file path
                <JIRA_ISSUE>-<N>.patch to update the patch file.

      4. Update the spec file. Add a new `Patch` tag for every patch in <UPSTREAM_PATCHES>.
         Add the new `Patch` tag after all existing `Patch` tags and, if `Patch` tags are numbered,
         make sure it has the highest number. Make sure the patch is applied in the "%prep" section
         and the `-p` argument is correct. Add an upstream URL as a comment above
         the `Patch:` tag - this URL references the related upstream commit or a pull/merge request.
         Include every patch defined in <UPSTREAM_PATCHES> list.
         IMPORTANT: Only ADD new patches. Do NOT modify existing Patch tags or their order.

      5. Run `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> prep` to see if the new patch
         applies cleanly. When `prep` command finishes with "exit 0", it's a success. Ignore errors from
         libtoolize that warn about newer files: "use '--force' to overwrite".

      6. Generate a SRPM using `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> srpm`.


      General instructions:

      - If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
      - Never change anything in the spec file changelog.
      - Preserve existing formatting and style conventions in spec files and patch headers.
      - Prefer native tools, if available, the `run_shell_command` tool should be the last resort.
      - Ignore all changes that cause conflicts in the following kinds of files: .github/ workflows, .gitignore, news, changes, and internal documentation.
      - Apply all changes that modify the core library of the package, and all binaries, manpages, and user-facing documentation.
      - For more information how the package is being built, inspect the RPM spec file and read sections `%prep` and `%build`.
      - If there is a complex conflict, you are required to properly resolve it by applying the core functionality of the proposed patch.
      - When a tool explicitly says "Abort cherry-pick approach, use git am workflow", immediately switch to approach B.
      - When using the cherry-pick workflow, you have access to <UPSTREAM_REPO> (the cloned upstream repository).
        You can explore it to find clues for resolving conflicts: examine commit history, related changes,
        documentation, test files, or similar fixes that might help understand the proper resolution.
      - Never apply the patches yourself, always use the `git_patch_apply` tool.
      - Never run `git am --skip`, always use the `git_apply_finish` tool instead.
      - Never abort the existing git am session.
    """


def get_prompt() -> str:
    return """
      Your working directory is {{local_clone}}, a clone of dist-git repository of package {{package}}.
      {{dist_git_branch}} dist-git branch has been checked out. You are working on Jira issue {{jira_issue}}
      {{#cve_id}}(a.k.a. {{.}}){{/cve_id}}.
      {{^build_error}}
      Backport upstream patches:
      {{#upstream_patches}}
      - {{.}}
      {{/upstream_patches}}
      Unpacked upstream sources are in {{unpacked_sources}}.
      {{/build_error}}
      {{#build_error}}
      This is a repeated backport, after the previous attempt the generated SRPM failed to build:

      {{.}}

      Everything from the previous attempt has been reset. Start over, follow the instructions from the start
      and don't forget to fix the issue.
      {{/build_error}}
    """


def get_fix_build_error_prompt() -> str:
    return """
      Your working directory is {{local_clone}}, a clone of dist-git repository of package {{package}}.
      {{dist_git_branch}} dist-git branch has been checked out. You are working on Jira issue {{jira_issue}}
      {{#cve_id}}(a.k.a. {{.}}){{/cve_id}}.

      Upstream patches that were backported:
      {{#upstream_patches}}
      - {{.}}
      {{/upstream_patches}}

      The backport of upstream patches was initially successful using the cherry-pick workflow,
      but the build failed with the following error:

      {{build_error}}

      CRITICAL: The upstream repository ({{local_clone}}-upstream) still exists with all your previous work intact.
      DO NOT clone it again. DO NOT reset to base commit. DO NOT modify anything in {{local_clone}} dist-git repository.
      Your cherry-picked commits are still there in {{local_clone}}-upstream.

      Your task is to fix this build error by exploring the upstream repository and finding the best solution.
      Make ONE attempt to fix the issue - you will be called again if the build still fails.

      Follow these steps:

      STEP 1: Analyze the build error
      - Identify what's missing: undefined functions, types, macros, symbols, headers, or API changes
      - Look for patterns like "undefined reference", "implicit declaration", "undeclared identifier", etc.
      - Note the specific names of missing symbols

      STEP 2: Explore the upstream repository for solutions
      You have FULL ACCESS to the upstream repository ({{local_clone}}-upstream) as a reference:

      - Examine the history between versions:
        * `git -C {{local_clone}}-upstream log --oneline <base_version>..<target_commit>`

      - Search for how missing symbols are implemented:
        * Search in commit messages: `git -C {{local_clone}}-upstream log --all --grep="function_name" --oneline`
        * Search in code changes: `git -C {{local_clone}}-upstream log --all -S"function_name" --oneline`
        * Show commit details: `git -C {{local_clone}}-upstream show <commit_hash>`

      - Look at current implementation in newer versions:
        * View files to see how things work: `view` tool on files in {{local_clone}}-upstream
        * Understand the context and dependencies
        * See how the code evolved over time

      - Explore related changes:
        * Check header files, documentation, tests
        * Look for API changes, refactorings, helper functions
        * Understand the bigger picture

      STEP 3: Choose the best fix approach
      You have TWO options for fixing the issue:

      OPTION A: Cherry-pick prerequisite commits
      - If you find clean, self-contained commits that add what's missing
      - Use `cherry_pick_commit` tool ONE commit at a time (chronological order, oldest first)
      - Resolve conflicts using `str_replace` tool
      - Stage resolved files: `git -C {{local_clone}}-upstream add <file>`
      - Complete cherry-pick: use `cherry_pick_continue` tool

      OPTION B: Manually adapt the code
      - If cherry-picking would pull in too many dependencies
      - If the commit doesn't apply cleanly and needs significant adaptation
      - If you need to backport just a small piece of functionality
      - Directly edit files in {{local_clone}}-upstream using `str_replace` or `insert` tools
      - Make minimal changes to fix the specific build error
      - Commit your changes: `git -C {{local_clone}}-upstream add <files>` then
        `git -C {{local_clone}}-upstream commit -m "Manually backport: <description>"`

      You can MIX both approaches:
      - Cherry-pick some commits, then manually adapt code where needed
      - Use the upstream repo as a reference while writing your own backport

      STEP 4: Regenerate the patch
      - After making your fixes (cherry-picked or manual), regenerate the patch file
      - Use `generate_patch_from_commit` tool with the PATCHED_BASE commit
      - This creates a single patch with all changes: original commits + prerequisites/fixes
      - Overwrite {{jira_issue}}.patch in {{local_clone}}

      STEP 5: Test the build
      - The spec file should already reference {{jira_issue}}.patch
      - Run `centpkg --name={{package}} --namespace=rpms --release={{dist_git_branch}} prep` to verify patch applies
      - Run `centpkg --name={{package}} --namespace=rpms --release={{dist_git_branch}} srpm` to generate SRPM
      - Test if the SRPM builds successfully using the `build_package` tool:
        * Call build_package with the SRPM path, dist_git_branch, and jira_issue
        * Wait for build results
        * If build PASSES: Report success=true with the SRPM path
        * If build FAILS: Use `download_artifacts` to get build logs if available
        * Extract the new error message from the logs and report success=false with the error

      Report your results:
      - If build passes → Report success=true with the SRPM path
      - If build fails → Report success=false with the extracted error message
      - If you can't find a fix → Report success=false explaining why

      IMPORTANT RULES:
      - Work in the EXISTING {{local_clone}}-upstream directory (don't clone again)
      - Don't modify anything in {{local_clone}} dist-git except regenerating {{jira_issue}}.patch
      - You can freely explore, edit, commit in the upstream repo - it's your workspace
      - Use the upstream repo as a rich source of information and examples
      - Be creative and pragmatic - the goal is a working build, not perfect git history
      - Make ONE solid attempt to fix the issue - if the build fails, report the error clearly
      - Your work will persist in the upstream repo for the next attempt if needed

      Remember: Unpacked upstream sources are in {{unpacked_sources}}.
      The upstream repository at {{local_clone}}-upstream is your playground - explore it freely!
    """


def create_backport_agent(
    mcp_tools: list[Tool], local_tool_options: dict[str, Any], include_build_tools: bool = False
) -> RequirementAgent:
    """
    Create a backport agent.

    Args:
        mcp_tools: List of MCP gateway tools
        local_tool_options: Options for local tools
        include_build_tools: If True, include build_package and download_artifacts tools
                           for iterative build testing during error fixing
    """
    base_tools = [
        ThinkTool(),
        DuckDuckGoSearchTool(),
        RunShellCommandTool(options=local_tool_options),
        CreateTool(options=local_tool_options),
        ViewTool(options=local_tool_options),
        InsertTool(options=local_tool_options),
        InsertAfterSubstringTool(options=local_tool_options),
        StrReplaceTool(options=local_tool_options),
        SearchTextTool(options=local_tool_options),
        GetCWDTool(options=local_tool_options),
        RemoveTool(options=local_tool_options),
        GitPatchCreationTool(options=local_tool_options),
        GitPatchApplyTool(options=local_tool_options),
        GitPatchApplyFinishTool(options=local_tool_options),
        GitLogSearchTool(options=local_tool_options),
        GitPreparePackageSources(options=local_tool_options),
        # Upstream cherry-pick workflow tools
        GetPackageInfoTool(options=local_tool_options),
        ExtractUpstreamRepositoryTool(options=local_tool_options),
        CloneUpstreamRepositoryTool(options=local_tool_options),
        FindBaseCommitTool(options=local_tool_options),
        ApplyDownstreamPatchesTool(options=local_tool_options),
        CherryPickCommitTool(options=local_tool_options),
        CherryPickContinueTool(options=local_tool_options),
        GeneratePatchFromCommitTool(options=local_tool_options),
    ]

    # Add build tools if requested (for iterative build error fixing)
    if include_build_tools:
        base_tools.extend([t for t in mcp_tools if t.name in ["build_package", "download_artifacts"]])

    return RequirementAgent(
        name="BackportAgent",
        llm=get_chat_model(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=base_tools,
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                force_after=Tool,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(),
        # role and instructions above set defaults for the system prompt input
        # but the `RequirementAgentSystemPrompt` instance is shared so the defaults
        # affect all requirement agents - use our own copy to prevent that
        templates={"system": copy.deepcopy(RequirementAgentSystemPrompt)},
    )


def get_unpacked_sources(local_clone: Path, package: str) -> Path:
    """
    Get a path to the root of extracted archive directory tree (referenced as TLD
    in RPM documentation) for a given package.

    That's the place where we'll initiate the backporting process.
    """
    with Specfile(local_clone / f"{package}.spec") as spec:
        buildsubdir = spec.expand("%{buildsubdir}")
    if "/" in buildsubdir:
        # Sooner or later we'll run into a package where this will break. Sorry.
        # More details: https://github.com/packit/jotnar/issues/217
        buildsubdir = buildsubdir.split("/")[0]
    sources_dir = local_clone / buildsubdir

    if not sources_dir.exists():
        raise ValueError(f"Unpacked source directory does not exist: {sources_dir}")

    return sources_dir


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    max_build_attempts = int(os.getenv("MAX_BUILD_ATTEMPTS", "10"))
    # When using cherry-pick workflow, allow the same number of incremental fix attempts as build attempts
    # since the agent iterates internally to fix build errors
    max_incremental_fix_attempts = int(os.getenv("MAX_INCREMENTAL_FIX_ATTEMPTS", str(max_build_attempts)))

    local_tool_options = {"working_directory": None}

    class State(PackageUpdateState):
        upstream_patches: list[str]
        cve_id: str | None
        unpacked_sources: Path | None = Field(default=None)
        backport_log: list[str] = Field(default=[])
        backport_result: BackportOutputSchema | None = Field(default=None)
        attempts_remaining: int = Field(default=max_build_attempts)
        used_cherry_pick_workflow: bool = Field(default=False)  # Track if cherry-pick was used
        incremental_fix_attempts: int = Field(default=0)  # Track how many times we tried incremental fix

    async def run_workflow(
        package, dist_git_branch, upstream_patches, jira_issue, cve_id, redis_conn=None
    ):
        local_tool_options["working_directory"] = None

        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            backport_agent = create_backport_agent(gateway_tools, local_tool_options)
            build_agent = create_build_agent(gateway_tools, local_tool_options)
            log_agent = create_log_agent(gateway_tools, local_tool_options)

            workflow = Workflow(State, name="BackportWorkflow")

            async def change_jira_status(state):
                if not dry_run:
                    try:
                        await tasks.change_jira_status(
                            jira_issue=state.jira_issue,
                            status="In Progress",
                            available_tools=gateway_tools,
                        )
                    except Exception as status_error:
                        logger.warning(f"Failed to change status for {state.jira_issue}: {status_error}")
                else:
                    logger.info(f"Dry run: would change status of {state.jira_issue} to In Progress")
                return "fork_and_prepare_dist_git"

            async def fork_and_prepare_dist_git(state):
                # Reset workflow flags since we're starting fresh
                state.used_cherry_pick_workflow = False
                state.incremental_fix_attempts = 0

                state.local_clone, state.update_branch, state.fork_url, _ = await tasks.fork_and_prepare_dist_git(
                    jira_issue=state.jira_issue,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    available_tools=gateway_tools,
                )
                local_tool_options["working_directory"] = state.local_clone
                centpkg_cmd = ["centpkg", f"--name={state.package}", "--namespace=rpms", f"--release={state.dist_git_branch}"]
                await check_subprocess(centpkg_cmd + ["sources"], cwd=state.local_clone)
                await check_subprocess(centpkg_cmd + ["prep"], cwd=state.local_clone)
                state.unpacked_sources = get_unpacked_sources(state.local_clone, state.package)
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    for idx, upstream_patch in enumerate(state.upstream_patches):
                        # should we guess the patch name with log agent?
                        patch_name = f"{state.jira_issue}-{idx}.patch"
                        async with session.get(upstream_patch) as response:
                            if response.status < 400:
                                (state.local_clone / patch_name).write_text(await response.text())
                            else:
                                raise ValueError(f"Failed to fetch upstream patch: {response.status}")
                return "run_backport_agent"

            async def run_backport_agent(state):
                response = await backport_agent.run(
                    render_prompt(
                        template=get_prompt(),
                        input=BackportInputSchema(
                            local_clone=state.local_clone,
                            unpacked_sources=state.unpacked_sources,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                            cve_id=state.cve_id,
                            upstream_patches=state.upstream_patches,
                            build_error=state.build_error,
                        ),
                    ),
                    expected_output=BackportOutputSchema,
                    **get_agent_execution_config(),
                )
                state.backport_result = BackportOutputSchema.model_validate_json(response.last_message.text)
                if state.backport_result.success:
                    state.backport_log.append(state.backport_result.status)

                    # Detect if cherry-pick workflow was used by checking for upstream repo with commits
                    upstream_repo = Path(f"{state.local_clone}-upstream")
                    if upstream_repo.exists():
                        try:
                            result = await check_subprocess(
                                ["git", "-C", str(upstream_repo), "rev-list", "--count", "HEAD"],
                                capture_output=True
                            )
                            commit_count = int(result.stdout.strip())
                            if commit_count > 1:  # More than just initial commit
                                state.used_cherry_pick_workflow = True
                                logger.info(f"Cherry-pick workflow detected: {commit_count} commits in upstream repo")
                            else:
                                state.used_cherry_pick_workflow = False
                                logger.info("Git am workflow detected: no commits in upstream repo")
                        except Exception as e:
                            logger.warning(f"Could not determine workflow type: {e}")
                            state.used_cherry_pick_workflow = False
                    else:
                        state.used_cherry_pick_workflow = False
                        logger.info("Git am workflow detected: no upstream repo exists")

                    return "run_build_agent"
                else:
                    return "comment_in_jira"

            async def fix_build_error(state):
                """Try to fix build errors by finding and cherry-picking prerequisite commits.

                The agent will be called iteratively, with each attempt trying to fix the build error.
                The workflow loop ensures we keep trying until we succeed or exhaust attempts.
                """
                # We only reach here if cherry-pick workflow was used (state.used_cherry_pick_workflow == True)
                logger.info(f"Attempting incremental fix for cherry-pick workflow (attempt {state.incremental_fix_attempts}/{max_incremental_fix_attempts})")

                try:
                    # Create a fresh backport agent with build tools enabled for iterative testing
                    fix_agent = create_backport_agent(gateway_tools, local_tool_options, include_build_tools=True)

                    # Give the agent the current build error and let it try to fix it
                    response = await fix_agent.run(
                        render_prompt(
                            template=get_fix_build_error_prompt(),
                            input=BackportInputSchema(
                                local_clone=state.local_clone,
                                unpacked_sources=state.unpacked_sources,
                                package=state.package,
                                dist_git_branch=state.dist_git_branch,
                                jira_issue=state.jira_issue,
                                cve_id=state.cve_id,
                                upstream_patches=state.upstream_patches,
                                build_error=state.build_error,
                            ),
                        ),
                        expected_output=BackportOutputSchema,
                        **get_agent_execution_config(),
                    )

                    fix_result = BackportOutputSchema.model_validate_json(response.last_message.text)

                    if fix_result.success:
                        # Build passed! Update state and proceed
                        state.backport_result = fix_result
                        state.backport_log.append(fix_result.status)
                        logger.info("Incremental fix succeeded with passing build")
                        state.incremental_fix_attempts = 0  # Reset for potential future failures
                        return "update_release"

                    # Build still failing - update the error for next iteration
                    logger.info(f"Build still failing after fix attempt: {fix_result.error}")
                    state.build_error = fix_result.error
                    state.backport_result = fix_result

                    # Check if we should try again
                    state.incremental_fix_attempts += 1
                    if state.incremental_fix_attempts < max_incremental_fix_attempts:
                        logger.info(f"Will retry incremental fix (attempt {state.incremental_fix_attempts + 1}/{max_incremental_fix_attempts})")
                        return "fix_build_error"  # Try again with the new error
                    else:
                        # Exhausted all incremental fix attempts - give up
                        logger.error(f"Exhausted all {max_incremental_fix_attempts} incremental fix attempts, giving up")
                        state.backport_result.success = False
                        state.backport_result.error = (
                            f"Unable to fix build errors after {max_incremental_fix_attempts} incremental fix attempts. "
                            f"Last error: {fix_result.error}"
                        )
                        return "comment_in_jira"

                except Exception as e:
                    # If anything goes wrong in fix_build_error, give up
                    logger.error(f"Exception during incremental fix: {e}", exc_info=True)
                    state.backport_result.success = False
                    state.backport_result.error = f"Exception during incremental fix: {str(e)}"
                    return "comment_in_jira"

            async def run_build_agent(state):
                # Ensure we have a valid backport result with SRPM path
                if not state.backport_result or not state.backport_result.srpm_path:
                    logger.error("Cannot run build agent: no valid backport result or SRPM path")
                    state.backport_result = state.backport_result or BackportOutputSchema(
                        success=False,
                        srpm_path=None,
                        status="",
                        error="No SRPM generated by backport agent"
                    )
                    return "comment_in_jira"

                # Create a fresh build agent instance to avoid state issues when called multiple times
                fresh_build_agent = create_build_agent(gateway_tools, local_tool_options)
                response = await fresh_build_agent.run(
                    render_prompt(
                        template=get_build_prompt(),
                        input=BuildInputSchema(
                            srpm_path=state.backport_result.srpm_path,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                        ),
                    ),
                    expected_output=BuildOutputSchema,
                    **get_agent_execution_config(),
                )
                build_result = BuildOutputSchema.model_validate_json(response.last_message.text)
                if build_result.success:
                    # Build succeeded - reset incremental fix counter for potential future failures
                    state.incremental_fix_attempts = 0
                    return "update_release"
                if build_result.is_timeout:
                    logger.info(f"Build timed out for {state.jira_issue}, proceeding")
                    return "update_release"
                state.attempts_remaining -= 1
                if state.attempts_remaining <= 0:
                    state.backport_result.success = False
                    state.backport_result.error = (
                        f"Unable to successfully build the package in {max_build_attempts} attempts"
                    )
                    return "comment_in_jira"
                state.build_error = build_result.error
                # Try to fix build error incrementally if cherry-pick workflow was used
                if state.used_cherry_pick_workflow:
                    logger.info(f"Cherry-pick workflow was used - starting incremental fix")
                    return "fix_build_error"
                else:
                    # Git am workflow was used - reset and try again
                    logger.info("Git am workflow was used - resetting for retry")
                    return "fork_and_prepare_dist_git"

            async def update_release(state):
                try:
                    await tasks.update_release(
                        local_clone=state.local_clone,
                        package=state.package,
                        dist_git_branch=state.dist_git_branch,
                        rebase=False,
                    )
                except Exception as e:
                    logger.warning(f"Error updating release: {e}")
                    state.backport_result.success = False
                    state.backport_result.error = f"Could not update release: {e}"
                    return "comment_in_jira"
                return "stage_changes"

            async def stage_changes(state):
                try:
                    await tasks.stage_changes(
                        local_clone=state.local_clone,
                        files_to_commit=[f"{state.package}.spec", f"{state.jira_issue}.patch"],
                    )
                except Exception as e:
                    logger.warning(f"Error staging changes: {e}")
                    state.backport_result.success = False
                    state.backport_result.error = f"Could not stage changes: {e}"
                    return "comment_in_jira"
                if state.log_result:
                    return "commit_push_and_open_mr"
                return "run_log_agent"

            async def run_log_agent(state):
                response = await log_agent.run(
                    render_prompt(
                        template=get_log_prompt(),
                        input=LogInputSchema(
                            jira_issue=state.jira_issue,
                            changes_summary=state.backport_log[-1],
                        ),
                    ),
                    expected_output=LogOutputSchema,
                    **get_agent_execution_config(),
                )
                log_output = LogOutputSchema.model_validate_json(response.last_message.text)

                if redis_conn and not dry_run:
                    # Cache MR metadata for sharing MR titles
                    # for the same upstream fix across different streams if redis
                    # is available.
                    # Do not modify the cache during a dry run.
                    log_output = await tasks.cache_mr_metadata(
                        redis_conn,
                        log_output=log_output,
                        operation_type="backport",
                        package=state.package,
                        details=str(state.upstream_patches),
                    )
                state.log_result = log_output

                return "stage_changes"

            async def commit_push_and_open_mr(state):
                try:
                    formatted_patches = "\n".join(f" - {p}" for p in state.upstream_patches)
                    state.merge_request_url, state.merge_request_newly_created = await tasks.commit_push_and_open_mr(
                        local_clone=state.local_clone,
                        commit_message=(
                            f"{state.log_result.title}\n\n"
                            f"{state.log_result.description}\n\n"
                            + (f"CVE: {state.cve_id}\n" if state.cve_id else "")
                            + "Upstream patches:\n" + formatted_patches + "\n"
                            + f"Resolves: {state.jira_issue}\n\n"
                            f"This commit was backported {I_AM_JOTNAR}\n\n"
                            "Assisted-by: Jotnar\n"
                        ),
                        fork_url=state.fork_url,
                        dist_git_branch=state.dist_git_branch,
                        update_branch=state.update_branch,
                        mr_title=state.log_result.title,
                        mr_description=(
                            f"This merge request was created {I_AM_JOTNAR}\n"
                            f"{CAREFULLY_REVIEW_CHANGES}\n\n"
                            f"{state.log_result.description}\n\n"
                            + "Upstream patches:\n" + formatted_patches + "\n"
                            f"Resolves: {state.jira_issue}\n\n"
                            f"Backporting steps:\n\n{state.backport_log[-1]}"
                        ),
                        available_tools=gateway_tools,
                        commit_only=dry_run,
                    )
                except Exception as e:
                    logger.warning(f"Error committing and opening MR: {e}")
                    state.merge_request_url = None
                    state.backport_result.success = False
                    state.backport_result.error = f"Could not commit and open MR: {e}"
                return "add_blocking_comment"

            async def add_blocking_comment(state):
                return await PackageUpdateStep.add_blocking_comment(
                    state, "create_merge_request_checklist", dry_run=dry_run, gateway_tools=gateway_tools
                )

            async def create_merge_request_checklist(state):
                return await PackageUpdateStep.create_merge_request_checklist(
                    state, "add_fusa_label", dry_run=dry_run, gateway_tools=gateway_tools)

            async def add_fusa_label(state):
                return await PackageUpdateStep.add_fusa_label(state, "comment_in_jira", dry_run=dry_run, gateway_tools=gateway_tools)

            async def comment_in_jira(state):
                if dry_run:
                    return Workflow.END
                if state.backport_result.success:
                    comment_text = (
                        state.merge_request_url
                        if state.merge_request_url
                        else state.backport_result.status
                    )
                else:
                    comment_text = f"Agent failed to perform a backport: {state.backport_result.error}"
                logger.info(f"Result to be put in Jira comment: {comment_text}")
                await tasks.comment_in_jira(
                    jira_issue=state.jira_issue,
                    agent_type="Backport",
                    comment_text=comment_text,
                    available_tools=gateway_tools,
                )
                return Workflow.END

            workflow.add_step("change_jira_status", change_jira_status)
            workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
            workflow.add_step("run_backport_agent", run_backport_agent)
            workflow.add_step("fix_build_error", fix_build_error)
            workflow.add_step("run_build_agent", run_build_agent)
            workflow.add_step("update_release", update_release)
            workflow.add_step("stage_changes", stage_changes)
            workflow.add_step("run_log_agent", run_log_agent)
            workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
            workflow.add_step("add_blocking_comment", add_blocking_comment)
            workflow.add_step("create_merge_request_checklist", create_merge_request_checklist)
            workflow.add_step("add_fusa_label", add_fusa_label)
            workflow.add_step("comment_in_jira", comment_in_jira)

            response = await workflow.run(
                State(
                    package=package,
                    dist_git_branch=dist_git_branch,
                    upstream_patches=upstream_patches,
                    jira_issue=jira_issue,
                    cve_id=cve_id,
                ),
            )
            return response.state

    if (
        (package := os.getenv("PACKAGE", None))
        and (branch := os.getenv("BRANCH", None))
        and (upstream_patches_raw := os.getenv("UPSTREAM_PATCHES", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
    ):
        upstream_patches = upstream_patches_raw.split(",")
        logger.info("Running in direct mode with environment variables")
        state = await run_workflow(
            package=package,
            dist_git_branch=branch,
            upstream_patches=upstream_patches,
            jira_issue=jira_issue,
            cve_id=os.getenv("CVE_ID", None),
            redis_conn=None,
        )
        logger.info(f"Direct run completed: {state.backport_result.model_dump_json(indent=4)}")
        return

    logger.info("Starting backport agent in queue mode")
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        # Determine which backport queue to listen to based on container version
        container_version = os.getenv("CONTAINER_VERSION", "c10s")
        backport_queue = RedisQueues.BACKPORT_QUEUE_C9S.value if container_version == "c9s" else RedisQueues.BACKPORT_QUEUE_C10S.value
        logger.info(f"Connected to Redis, max retries set to {max_retries}, listening to queue: {backport_queue}")

        while True:
            logger.info(f"Waiting for tasks from {backport_queue} (timeout: 30s)...")
            element = await fix_await(redis.brpop([backport_queue], timeout=30))
            if element is None:
                logger.info("No tasks received, continuing to wait...")
                continue

            _, payload = element
            logger.info("Received task from queue.")

            task = Task.model_validate_json(payload)
            triage_state = task.metadata
            backport_data = BackportData.model_validate(triage_state["triage_result"]["data"])
            dist_git_branch = triage_state["target_branch"]
            logger.info(
                f"Processing backport for package: {backport_data.package}, "
                f"JIRA: {backport_data.jira_issue}, branch: {dist_git_branch}, "
                f"attempt: {task.attempts + 1}"
            )

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {backport_data.jira_issue}"
                    )
                    await fix_await(redis.lpush(backport_queue, task.model_dump_json()))
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, "
                        f"moving to error list: {backport_data.jira_issue}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORT_ERRORED.value],
                        labels_to_remove=[JiraLabels.BACKPORT_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            try:
                logger.info(f"Starting backport processing for {backport_data.jira_issue}")
                state = await run_workflow(
                    package=backport_data.package,
                    dist_git_branch=dist_git_branch,
                    upstream_patches=backport_data.patch_urls,
                    jira_issue=backport_data.jira_issue,
                    cve_id=backport_data.cve_id,
                    redis_conn=redis,
                )
                logger.info(
                    f"Backport processing completed for {backport_data.jira_issue}, " f"success: {state.backport_result.success}"
                )

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                await retry(task, ErrorData(details=error, jira_issue=backport_data.jira_issue).model_dump_json())
            else:
                if state.backport_result.success:
                    logger.info(f"Backport successful for {backport_data.jira_issue}, " f"adding to completed list")
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORTED.value],
                        labels_to_remove=[
                            JiraLabels.BACKPORT_IN_PROGRESS.value,
                            JiraLabels.BACKPORT_ERRORED.value,
                            JiraLabels.BACKPORT_FAILED.value,
                        ],
                        dry_run=dry_run
                    )
                    await redis.lpush(RedisQueues.COMPLETED_BACKPORT_LIST.value, state.backport_result.model_dump_json())
                else:
                    logger.warning(f"Backport failed for {backport_data.jira_issue}: {state.backport_result.error}")
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORT_FAILED.value],
                        labels_to_remove=[JiraLabels.BACKPORT_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    await retry(task, state.backport_result.error)


if __name__ == "__main__":
    try:
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
