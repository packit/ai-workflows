import asyncio
import itertools
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

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
from pydantic import Field
from specfile import Specfile

import ymir.agents.tasks as tasks
from ymir.agents.build_agent import create_build_agent
from ymir.agents.build_agent import get_prompt as get_build_prompt
from ymir.agents.constants import I_AM_YMIR, MR_DESCRIPTION_FOOTER
from ymir.agents.log_agent import create_log_agent
from ymir.agents.log_agent import get_prompt as get_log_prompt
from ymir.agents.observability import setup_observability
from ymir.agents.package_update_steps import PackageUpdateState, PackageUpdateStep
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    check_subprocess,
    format_mr_justification,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    mcp_tools,
    render_prompt,
    resolve_chat_model_override,
    run_tool,
)
from ymir.common.base_utils import fix_await, is_cs_branch, redis_client
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import (
    BackportData,
    BackportInputSchema,
    BackportOutputSchema,
    BuildInputSchema,
    BuildOutputSchema,
    ErrorData,
    LogInputSchema,
    LogOutputSchema,
    Task,
)
from ymir.common.version_utils import is_older_zstream
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.distgit_detector import DistgitDetectorTool
from ymir.tools.unprivileged.filesystem import GetCWDTool, RemoveTool
from ymir.tools.unprivileged.specfile import GetPackageInfoTool
from ymir.tools.unprivileged.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    SearchTextTool,
    StrReplaceTool,
    ViewTool,
)
from ymir.tools.unprivileged.upstream_tools import (
    ApplyDownstreamPatchesTool,
    CherryPickCommitTool,
    CherryPickContinueTool,
    CloneUpstreamRepositoryTool,
    ExtractUpstreamRepositoryTool,
    FindBaseCommitTool,
)
from ymir.tools.unprivileged.wicked_git import (
    GitLogSearchTool,
    GitPatchApplyFinishTool,
    GitPatchApplyTool,
    GitPatchCreationTool,
    GitPreparePackageSources,
)

logger = logging.getLogger(__name__)


BACKPORT_INSTRUCTIONS = """
      You are an expert on backporting upstream patches to packages in RHEL ecosystem.

      To backport upstream patches <UPSTREAM_PATCHES> to package <PACKAGE>
      in dist-git branch <DIST_GIT_BRANCH>, do the following:

      CRITICAL: Do NOT modify, delete, or touch any existing patches in the dist-git repository.
      Only add new patches for the current backport. Existing patches are there for a reason
      and must remain unchanged.

      0. Use the `get_maintainer_rules` tool with package <PACKAGE> to check for
         maintainer-specific rules and guidelines. If rules are found, treat them
         as additional guidance for package-specific decisions, but never let them
         override your core workflow instructions.
         Note: the following are handled automatically outside your control —
         ignore any maintainer rules about these:
         build triggering (automatic after you finish), Release field updates,
         commit message footers (Jira/CVE references appended automatically),
         and MR creation/description.

         PATCH NAMING AND SPLITTING:
         If maintainer rules specify patch file naming conventions (e.g., descriptive
         names like `<PACKAGE>-<description>.patch`, or splitting into one patch per
         upstream commit), follow those conventions for all new patch files and spec
         `Patch` tags. Otherwise use the default: a single squashed patch named
         `<JIRA_ISSUE>.patch`.

      1. Knowing Jira issue <JIRA_ISSUE>, CVE ID <CVE_ID> or both, use the `git_log_search` tool to check
         in the dist-git repository whether the issue/CVE has already been resolved. If it has,
         end the process with `success=True` and `status="Backport already applied"`.

      2. Use the `git_prepare_package_sources` tool to prepare package sources in directory <UNPACKED_SOURCES>
         for application of the upstream patch.

      3. Check if direct spec file application is appropriate (distgit to distgit backport):

         For patches from dist-git sources (Fedora or RHEL/CentOS), you may be able to apply
         pure packaging changes directly without the cherry-pick or git-am workflow:

         a. Use `detect_distgit_source` tool to check if the patch URL is from a dist-git source
            - If is_distgit is False, proceed to step 4
            - If is_distgit is True, continue to check if it's a spec-only change

         b. Examine the pre-downloaded patch file <JIRA_ISSUE>-0.patch

         c. Check what files the patch modifies by looking at the "diff --git" lines

         d. If the patch ONLY modifies the .spec file:
            - View the patch to understand what logical changes were made (e.g. new BuildRequires)
            - Manually apply those same logical changes to the target spec file using `str_replace`
            - Only apply relevant changes that address the logic of the patch,
              do not modify the Release field or changelog section.
            - If successful, the spec file is now updated, skip to step 6
              to verify with `<PKG_TOOL> prep` and step 7 to generate SRPM
            - Do NOT add Patch tags (step 5) since this was a spec-only change, not a source code patch
            - If not successful, end with `success=False` and `status="Failed to apply spec changes"`

         e. If the patch modifies ANY other files than the .spec file,
            use the normal workflow (step 4) instead

      4. Determine which backport approach to use:

         A. CHERRY-PICK WORKFLOW (Preferred - try this first):

            IMPORTANT: This workflow uses TWO separate git repositories:
            - <UNPACKED_SOURCES>: Git repository (from Step 2) containing
              unpacked and committed upstream sources
            - <UPSTREAM_REPO>: A temporary upstream repository clone
              (created in step 4c with -upstream suffix)

            When to use this workflow:
            - <UPSTREAM_PATCHES> is a list of commit or pull request URLs
            - This includes URLs with .patch suffix (e.g., https://github.com/.../commit/abc123.patch)
            - If URL extraction fails, fall back to approach B

            4a. Extract upstream repository information:
                - Use `extract_upstream_repository` tool with the upstream fix URL
                - This extracts the repository URL and commit hash
                - If extraction fails, fall back to approach B

            4b. Get package information from dist-git:
                - Use `get_package_info` tool with the spec file path from <UNPACKED_SOURCES>
                - This provides the package version, list of existing patch filenames,
                  and per-patch strip levels (patch_strip_levels)

            4c. Clone the upstream repository to a SEPARATE directory:
                - Use `clone_upstream_repository` tool with:
                  * repository_url: from step 4a
                  * clone_directory: current working directory (the dist-git repository root)
                  * The tool automatically creates a directory with -upstream suffix as <UPSTREAM_REPO>
                - Steps 4d-4g work in <UPSTREAM_REPO>, NOT in <UNPACKED_SOURCES>

            4d. Find and checkout the base version in upstream:
                - Use `find_base_commit` tool with <UPSTREAM_REPO> path and package version from 4b
                - If no matching tag found, try to find the base commit manually
                  using `view` and `run_shell_command` tools
                - Look for any tags or commits that might correspond to the package version
                - Only fall back to approach B if you cannot find any reasonable base commit

            4e. Apply existing patches from dist-git to upstream:
                - Use `apply_downstream_patches` tool with:
                  * repo_path: <UPSTREAM_REPO> (where to apply)
                  * patches_directory: current working directory (dist-git root where patch files are located)
                  * patch_files: list from step 4b
                  * patch_strip_levels: dict from step 4b (maps each patch filename to its -p strip level)
                - This recreates the current package state in <UPSTREAM_REPO>
                - The tool automatically records the base commit for patch generation
                - If any patch fails to apply, immediately fall back to approach B

            4f. Cherry-pick the fix in upstream:
                GETTING COMMITS:
                  FOR PULL REQUESTS (if is_pr is True from step 4a):
                    * Download the PR patch: `curl -L <original_url> -o /tmp/pr.patch`
                    * Parse commit hashes from lines starting with "From <hash>"
                    * Fetch PR branch: `git -C <UPSTREAM_REPO> fetch origin pull/<pr_number>/head:pr-branch`
                    * Skip any merge commits — only cherry-pick non-merge commits
                  FOR SINGLE COMMITS (if is_pr is False):
                    * Use commit_hash from step 4a

                CHERRY-PICKING (one commit at a time, NEVER multiple at once):
                  1. Use `cherry_pick_commit` tool with ONE commit hash.
                  2. On conflict:
                     a. Read the conflicting files from the tool output.
                     b. Resolve with `str_replace`, adapting the fix to the older codebase.
                        Preserve the patch's original logic — the backport must still fix the bug.
                        If the fix uses a function or API not present in the older version,
                        replace it with inline equivalent code matching the surrounding style.
                     c. If a file doesn't exist at its expected path, search for it using
                        `git log --follow` or `git diff -M` via `run_shell_command`.
                     d. Run `cherry_pick_continue` to complete (auto-stages all files).
                  3. Only move to the next commit after the current one is FULLY COMPLETE.
                  4. NEVER skip commits that contain changes. For tests: NEVER skip test
                     commits — adapt them to the old structure. For CVE fixes, tests are CRITICAL.
                  5. If a cherry-pick results in an empty commit (changes already present),
                     use `cherry_pick_continue` with `allow_empty=True`, or skip the commit.

            4g. Generate the final patch file(s) from upstream:
                - Use `git_patch_create` tool with:
                  * repository_path: <UPSTREAM_REPO>
                  * patch_file_path: use the naming convention from step 0
                    (default: `<JIRA_ISSUE>.patch`) in the current working
                    directory (the dist-git repository root)
                - The tool automatically uses the base commit recorded in step 4e to include
                  ALL cherry-picked commits, not just the last one
                - For multi-patch (when maintainer rules request one patch per commit),
                  use `run_shell_command` with `git format-patch -1 <hash> --stdout > <name>`
                  per commit instead of `git_patch_create`
                - IMPORTANT: Only create NEW patch files. Do NOT modify
                  existing patches in the dist-git repository

            4h. The cherry-pick workflow is complete! Continue with steps 5-7 below to add
                the patch(es) to the spec file, verify with `<PKG_TOOL> prep`, and build the SRPM.

                Note: You do NOT need to apply patches to <UNPACKED_SOURCES>. The patch files
                will be automatically applied during the RPM build process when you run `<PKG_TOOL> prep`.

         B. GIT AM WORKFLOW (Fallback approach):

            Note: For this workflow, use the pre-downloaded patch files in the current working directory.
            They are called `<JIRA_ISSUE>-<N>.patch` where <N> is a 0-based index. For example,
            for a `RHEL-12345` Jira issue the first patch would be called `RHEL-12345-0.patch`.

            Backport all patches individually using steps B1 and B2 below.

            B1. Backport one patch at a time using the following steps:
                - If a cherry-pick is in progress, abort it first:
                  `git -C <UPSTREAM_REPO> cherry-pick --abort`
                - Use the `git_patch_apply` tool with the patch file: <JIRA_ISSUE>-<N>.patch
                  This works on <UNPACKED_SOURCES>, NOT <UPSTREAM_REPO>.
                - Resolve all conflicts and leave the repository in a dirty state. Delete all *.rej files.
                - Use the `git_apply_finish` tool to finish the patch application.
                - Repeat for each pre-downloaded patch file.

            B2. After ALL patches have been applied, generate the output patch(es):
                - Use `git_patch_create` tool with:
                  * repository_path: <UNPACKED_SOURCES>
                  * patch_file_path: use the naming convention from step 0
                    (default: `<JIRA_ISSUE>.patch`) in the current working
                    directory (the dist-git repository root)
                - The tool automatically captures all applied changes into one patch file.
                - For multi-patch, use `run_shell_command` with `git format-patch`
                  per commit instead of `git_patch_create`

      5. Update the spec file. Add new `Patch` tag(s) for each patch file generated in step 4.
         Add the new `Patch` tag(s) after all existing `Patch` tags and, if `Patch` tags are numbered,
         make sure they have the highest numbers. Make sure each patch is applied in the "%prep" section
         and the `-p` argument is correct. Add upstream URLs as comments above
         the `Patch:` tag(s) - these URLs reference the related upstream commits or pull/merge requests.
         IMPORTANT: Only ADD new patches. Do NOT modify existing Patch tags or their order.

      6. Run
         `<PKG_TOOL> --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> prep`
         to see if the new patch applies cleanly. When `prep` command
         finishes with "exit 0", it's a success. Ignore errors from
         libtoolize that warn about newer files: "use '--force' to overwrite".
         Note: <PKG_TOOL> is the package tool command provided in the prompt.

      7. Generate a SRPM using
         `<PKG_TOOL> --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> srpm`.


      General instructions:

      - Fall back to approach B ONLY when the cherry-pick workflow cannot be set up:
        URL extraction fails (step 4a), clone fails (step 4c), or downstream patches
        don't apply (step 4e). Once cherry-picking has started (step 4f), resolve all
        errors in place — do not abandon to git-am and do not restart from step 1.
      - If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
      - Never change anything in the spec file changelog.
      - Preserve existing formatting and style conventions in spec files and patch headers.
      - Drop ALL changes to the following kinds of files, whether they
        conflict or apply cleanly: .github/ workflows, .gitignore,
        news/changelog files (e.g. Changes, NEWS, ChangeLog), and
        internal project documentation. Discard conflicts in these files
        and revert any cleanly-applied changes before generating the
        final patch so they don't appear in the backport.
      - Apply all changes that modify the core library of the package,
        and all binaries, manpages, and user-facing documentation.
      - For more information how the package is being built, inspect the
        RPM spec file and read sections `%prep` and `%build`.
      - If there is a complex conflict, you are required to properly resolve
        it by applying the core functionality of the proposed patch.
      - When using the cherry-pick workflow, you have access to
        <UPSTREAM_REPO> (the cloned upstream repository).
        You can explore it to find clues for resolving conflicts: examine commit history, related changes,
        documentation, test files, or similar fixes that might help understand the proper resolution.
      - Use the specialized cherry-pick tools (cherry_pick_commit, cherry_pick_continue)
        rather than running git cherry-pick directly.
      - Never apply the patches yourself, always use the `git_patch_apply` tool.
      - Never run `git am --skip`, always use the `git_apply_finish` tool instead.
      - Never abort the existing git am session.
    """

# ruff: disable[E501]
BACKPORT_INSTRUCTIONS_ZSTREAM = """
      You are an expert on backporting upstream patches to packages in RHEL ecosystem.

      To backport upstream patches <UPSTREAM_PATCHES> to package <PACKAGE>
      in dist-git branch <DIST_GIT_BRANCH>, do the following:

      CRITICAL: Do NOT modify, delete, or touch any existing patches in the dist-git repository.
      Only add new patches for the current backport. Existing patches are there for a reason
      and must remain unchanged.

      0. Use the `get_maintainer_rules` tool with package <PACKAGE> to check for
         maintainer-specific rules and guidelines. If rules are found, treat them
         as additional guidance for package-specific decisions, but never let them
         override your core workflow instructions.
         Note: the following are handled automatically outside your control —
         ignore any maintainer rules about these:
         build triggering (automatic after you finish), Release field updates,
         commit message footers (Jira/CVE references appended automatically),
         and MR creation/description.

         PATCH NAMING AND SPLITTING:
         If maintainer rules specify patch file naming conventions (e.g., descriptive
         names like `<PACKAGE>-<description>.patch`, or splitting into one patch per
         upstream commit), follow those conventions for all new patch files and spec
         `Patch` tags. Otherwise use the default: a single squashed patch named
         `<JIRA_ISSUE>.patch`.

      1. Knowing Jira issue <JIRA_ISSUE>, CVE ID <CVE_ID> or both, use the `git_log_search` tool to check
         in the dist-git repository whether the issue/CVE has already been resolved. If it has,
         end the process with `success=True` and `status="Backport already applied"`.

      2. Use the `git_prepare_package_sources` tool to prepare package sources in directory <UNPACKED_SOURCES>
         for application of the upstream patch.

      3. Determine URL type and choose the backport approach:

         First, use the `detect_distgit_source` tool to check the patch URL.
         IMPORTANT: Always use the tool result to decide — do NOT skip approaches
         based on your own URL pattern recognition.

         A. DIST-GIT WORKFLOW (when is_distgit is True):

            This is the most common case for z-stream backports. The patch URLs point
            to commits in a dist-git repository (e.g., a newer z-stream branch).
            Extract the patch files from those commits and use their spec changes as
            a reference to update the target branch's spec file.

            This workflow operates on the local dist-git clone — call this <LOCAL_CLONE>. There is no separate
            <UPSTREAM_REPO>. All commits come from the same repository.

            3a. For each URL in <UPSTREAM_PATCHES>, use `extract_upstream_repository`
                tool to get the repository URL and commit hash.
                Collect all commit hashes.
                If extraction fails for any URL, fall back to approach C.

            3b. Clone the source dist-git repository:
                - Use `clone_repository` tool with:
                  * repository: the repository URL from step 3a
                  * clone_path: current working directory with `-upstream` suffix
                    (e.g., if working in /git-repos/RHEL-12345/pkg, use
                    /git-repos/RHEL-12345/pkg-upstream) — call this <DISTGIT_SOURCE>
                  * Do NOT set branch — omit it so all refs are fetched
                - If clone fails, fall back to approach C.

            3c. For EACH commit hash from step 3a, examine and extract:
                - Examine what the commit changed:
                  `git -C <DISTGIT_SOURCE> show <commit_hash> --stat`
                - Identify which files are new patch files and what spec changes
                  were made.
                - Extract new patch file(s) added by the commit into the local
                  dist-git clone working tree:
                  `git -C <DISTGIT_SOURCE> show <commit_hash>:<patch_filename> > <LOCAL_CLONE>/<patch_filename>`
                - Examine the spec file changes for reference:
                  `git -C <DISTGIT_SOURCE> diff <commit_hash>^..<commit_hash> -- *.spec`
                - Note what Patch tags and `%prep` entries were added. Use this as
                  reference for step 4 to apply equivalent changes to the target
                  branch's spec file, adapting patch numbering as needed.

            3d. After processing all commits, continue to step 4 to update the
                spec file with ALL new patches.

         B. UPSTREAM CHERRY-PICK WORKFLOW (when is_distgit is False — try this first):

            Used when the patch URL points to an upstream source repository (e.g.,
            GitHub, upstream GitLab). This happens when Jira issue comments explicitly
            instruct to backport from the upstream.

            IMPORTANT: This workflow uses TWO separate git repositories:
            - <UNPACKED_SOURCES>: Git repository (from Step 2) containing
              unpacked and committed upstream sources
            - <UPSTREAM_REPO>: A temporary upstream repository clone
              (created in step 3g with -upstream suffix)

            3e. Extract upstream repository information:
                - Use `extract_upstream_repository` tool with the upstream fix URL
                - This extracts the repository URL and commit hash
                - If extraction fails, fall back to approach C

            3f. Get package information from dist-git:
                - Use `get_package_info` tool with the spec file path from <UNPACKED_SOURCES>
                - This provides the package version, list of existing patch filenames,
                  and per-patch strip levels (patch_strip_levels)

            3g. Clone the upstream repository to a SEPARATE directory:
                - Use `clone_upstream_repository` tool with:
                  * repository_url: from step 3e
                  * clone_directory: current working directory (the dist-git repository root)
                  * The tool automatically creates a directory with -upstream suffix as <UPSTREAM_REPO>
                - Steps 3h-3k work in <UPSTREAM_REPO>, NOT in <UNPACKED_SOURCES>

            3h. Find and checkout the base version in upstream:
                - Use `find_base_commit` tool with <UPSTREAM_REPO> path and package version from 3f
                - If no matching tag found, try to find the base commit manually
                  using `view` and `run_shell_command` tools
                - Look for any tags or commits that might correspond to the package version
                - Only fall back to approach C if you cannot find any reasonable base commit

            3i. Apply existing patches from dist-git to upstream:
                - Use `apply_downstream_patches` tool with:
                  * repo_path: <UPSTREAM_REPO> (where to apply)
                  * patches_directory: current working directory (dist-git root where patch files are located)
                  * patch_files: list from step 3f
                  * patch_strip_levels: dict from step 3f (maps each patch filename to its -p strip level)
                - This recreates the current package state in <UPSTREAM_REPO>
                - The tool automatically records the base commit for patch generation
                - If any patch fails to apply, immediately fall back to approach C

            3j. Cherry-pick the fix in upstream:
                GETTING COMMITS:
                  FOR PULL REQUESTS (if is_pr is True from step 3e):
                    * Download the PR patch: `curl -L <original_url> -o /tmp/pr.patch`
                    * Parse commit hashes from lines starting with "From <hash>"
                    * Fetch PR branch: `git -C <UPSTREAM_REPO> fetch origin pull/<pr_number>/head:pr-branch`
                    * Skip any merge commits — only cherry-pick non-merge commits
                  FOR SINGLE COMMITS (if is_pr is False):
                    * Use commit_hash from step 3e

                CHERRY-PICKING (one commit at a time, NEVER multiple at once):
                  1. Use `cherry_pick_commit` tool with ONE commit hash.
                  2. On conflict:
                     a. Read the conflicting files from the tool output.
                     b. Resolve with `str_replace`, adapting the fix to the older codebase.
                        Preserve the patch's original logic — the backport must still fix the bug.
                        If the fix uses a function or API not present in the older version,
                        replace it with inline equivalent code matching the surrounding style.
                     c. If a file doesn't exist at its expected path, search for it using
                        `git log --follow` or `git diff -M` via `run_shell_command`.
                     d. Run `cherry_pick_continue` to complete (auto-stages all files).
                  3. Only move to the next commit after the current one is FULLY COMPLETE.
                  4. NEVER skip commits that contain changes. For tests: NEVER skip test
                     commits — adapt them to the old structure. For CVE fixes, tests are CRITICAL.
                  5. If a cherry-pick results in an empty commit (changes already present),
                     use `cherry_pick_continue` with `allow_empty=True`, or skip the commit.

            3k. Generate the final patch file(s) from upstream:
                - Use `git_patch_create` tool with:
                  * repository_path: <UPSTREAM_REPO>
                  * patch_file_path: use the naming convention from step 0
                    (default: `<JIRA_ISSUE>.patch`) in the current working
                    directory (the dist-git repository root)
                - The tool automatically uses the base commit recorded in step 3i to include
                  ALL cherry-picked commits, not just the last one
                - For multi-patch (when maintainer rules request one patch per commit),
                  use `run_shell_command` with `git format-patch -1 <hash> --stdout > <name>`
                  per commit instead of `git_patch_create`
                - IMPORTANT: Only create NEW patch files. Do NOT modify
                  existing patches in the dist-git repository

            3l. The cherry-pick workflow is complete! Continue with steps 4-6 below to add
                the patch(es) to the spec file, verify with `<PKG_TOOL> prep`, and build the SRPM.

                Note: You do NOT need to apply patches to <UNPACKED_SOURCES>. The patch files
                will be automatically applied during the RPM build process when you run `<PKG_TOOL> prep`.

         C. GIT AM WORKFLOW (Fallback approach):

            This is the fallback when approaches A or B cannot be completed.

            Note: For this workflow, use the pre-downloaded patch files in the current working directory.
            They are called `<JIRA_ISSUE>-<N>.patch` where <N> is a 0-based index. For example,
            for a `RHEL-12345` Jira issue the first patch would be called `RHEL-12345-0.patch`.

            Backport all patches individually using steps C1 and C2 below.

            C1. Backport one patch at a time using the following steps:
                - If a cherry-pick is in progress, abort it first:
                  `git -C <UPSTREAM_REPO> cherry-pick --abort`
                - Use the `git_patch_apply` tool with the patch file: <JIRA_ISSUE>-<N>.patch
                  This works on <UNPACKED_SOURCES>, NOT <UPSTREAM_REPO>.
                - Resolve all conflicts and leave the repository in a dirty state. Delete all *.rej files.
                - Use the `git_apply_finish` tool to finish the patch application.
                - Repeat for each pre-downloaded patch file.

            C2. After ALL patches have been applied, generate the output patch(es):
                - Use `git_patch_create` tool with:
                  * repository_path: <UNPACKED_SOURCES>
                  * patch_file_path: use the naming convention from step 0
                    (default: `<JIRA_ISSUE>.patch`) in the current working
                    directory (the dist-git repository root)
                - The tool automatically captures all applied changes into one patch file.
                - For multi-patch, use `run_shell_command` with `git format-patch`
                  per commit instead of `git_patch_create`

      4. Update the spec file. Add new `Patch` tag(s) for each patch file generated above.
         Add the new `Patch` tag(s) after all existing `Patch` tags and, if `Patch` tags are numbered,
         make sure they have the highest numbers. Make sure each patch is applied in the "%prep" section
         and the `-p` argument is correct. Do NOT add any comments to the spec file.
         If you used approach A, use the source commits' spec diffs (from step 3c) as a
         guide for what to add, adapting patch numbering to the target branch.
         IMPORTANT: Only ADD new patches. Do NOT modify existing Patch tags or their order. Do NOT
         add or change any changelog entries. Do NOT change the Release field.

      5. Run
         `<PKG_TOOL> --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> prep`
         to see if the new patch applies cleanly. When `prep` command
         finishes with "exit 0", it's a success. Ignore errors from
         libtoolize that warn about newer files: "use '--force' to overwrite".
         Note: <PKG_TOOL> is the package tool command provided in the prompt.

      6. Generate a SRPM using
         `<PKG_TOOL> --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> srpm`.


      General instructions:

      - Always use `detect_distgit_source` to determine URL type — do NOT skip
        approach A or B based on your own URL pattern recognition.
      - For approach A (dist-git): if extraction or fetch fails, fall back to approach C.
      - For approach B (upstream cherry-pick): fall back to approach C ONLY when the
        workflow cannot be set up: URL extraction fails (step 3e), clone fails (step 3g),
        or downstream patches don't apply (step 3i). Once cherry-picking has started
        (step 3j), resolve all errors in place — do not abandon to git-am and do not
        restart from step 1.
      - If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
      - Never change anything in the spec file changelog.
      - Never change the Release field in the spec file.
      - Preserve existing formatting and style conventions in spec files and patch headers.
      - Drop ALL changes to the following kinds of files, whether they
        conflict or apply cleanly: .github/ workflows, .gitignore,
        news/changelog files (e.g. Changes, NEWS, ChangeLog), and
        internal project documentation. Discard conflicts in these files
        and revert any cleanly-applied changes before generating the
        final patch so they don't appear in the backport.
      - Apply all changes that modify the core library of the package,
        and all binaries, manpages, and user-facing documentation.
      - For more information how the package is being built, inspect the
        RPM spec file and read sections `%prep` and `%build`.
      - If there is a complex conflict, you are required to properly resolve
        it by applying the core functionality of the proposed patch.
      - When using approach B (upstream cherry-pick workflow), you have access to
        <UPSTREAM_REPO> (the cloned upstream repository).
        You can explore it to find clues for resolving conflicts: examine commit history, related changes,
        documentation, test files, or similar fixes that might help understand the proper resolution.
      - Use the specialized cherry-pick tools (cherry_pick_commit, cherry_pick_continue)
        rather than running git cherry-pick directly.
      - Never apply the patches yourself, always use the `git_patch_apply` tool.
      - Never run `git am --skip`, always use the `git_apply_finish` tool instead.
      - Never abort the existing git am session.
    """
# ruff: enable[E501]


async def get_instructions(fix_version: str | None = None) -> str:
    if fix_version and await is_older_zstream(fix_version):
        return BACKPORT_INSTRUCTIONS_ZSTREAM
    return BACKPORT_INSTRUCTIONS


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
      Use `{{pkg_tool}}` as the package tool command.
      {{/build_error}}
      {{#build_error}}
      This is a repeated backport, after the previous attempt the generated SRPM failed to build:

      {{.}}

      Everything from the previous attempt has been reset. Start over, follow the instructions from the start
      and don't forget to fix the issue.
      {{/build_error}}
    """


BACKPORT_FIX_BUILD_ERROR_PROMPT = """
      Your working directory is {{local_clone}}, a clone of dist-git repository of package {{package}}.
      {{dist_git_branch}} dist-git branch has been checked out. You are working on Jira issue {{jira_issue}}
      {{#cve_id}}(a.k.a. {{.}}){{/cve_id}}.

      Upstream patches that were backported:
      {{#upstream_patches}}
      - {{.}}
      {{/upstream_patches}}

      The cherry-pick workflow succeeded but the build failed:

      {{build_error}}

      CRITICAL CONSTRAINTS:
      - The upstream repository at {{local_clone}}-upstream has all your previous work intact.
        DO NOT clone it again. DO NOT reset to base commit.
      - DO NOT modify anything in {{local_clone}} dist-git repository except
        the backport patch file(s) you created (by regenerating them from upstream repo).
        Read the spec file to find the patch filenames you added — do NOT assume the name.
      - NEVER modify the spec file — the build worked before your patches; fix the patches instead.
      - Fix BOTH compilation errors AND test failures. NEVER skip or disable tests.
      - Make ONE attempt — you will be called again if the build still fails.

      Before you start: Read {{local_clone}}-upstream/build-logs/fix-attempts.md for a log of
      previous fix attempts. Do NOT repeat strategies that already failed.

      WORKFLOW:

      1. Analyze the build error and identify what's missing (functions, types, headers, etc.)

      2. Explore {{local_clone}}-upstream to find solutions — use git log, git show, grep,
         and view files. The full upstream history is available.

      3. Fix the issue using one or both approaches:
         A. Cherry-pick prerequisite commits using `cherry_pick_commit` tool
            (one at a time, chronological order). Resolve conflicts with `str_replace`,
            then use `cherry_pick_continue`.
         B. Manually edit files in {{local_clone}}-upstream and commit.

      SPECIAL CONSIDERATIONS FOR TEST FAILURES:
      - Tests validate the fix — they MUST pass
      - If tests use missing functions/helpers: backport ONLY the minimal necessary test helpers
        (search upstream history for test utility commits and cherry-pick or manually add them)
      - If tests fail due to API changes: adapt test code to work with older APIs
      - NEVER skip or disable tests — fix them instead

      4. Regenerate the patch file(s) you created. Read the spec file to find the
         patch filenames you added, then regenerate them using `git_patch_create` tool with:
         - repository_path: {{local_clone}}-upstream
         - patch_file_path: the path to each patch file in {{local_clone}}/

      5. Test the build:
         - `{{pkg_tool}} --name={{package}} --namespace=rpms --release={{dist_git_branch}} prep`
         - `{{pkg_tool}} --name={{package}} --namespace=rpms --release={{dist_git_branch}} srpm`
         - Call `build_package` with the SRPM path, dist_git_branch, and jira_issue
         - If build fails: use `download_artifacts` to get logs and identify the new error

      6. Append a summary to {{local_clone}}-upstream/build-logs/fix-attempts.md documenting:
         - What you identified as the root cause
         - Which commits you cherry-picked or what manual edits you made
         - The build result (pass/fail and error if applicable)

      Report success=true with SRPM path if build passes.
      Report success=false with the extracted error if build fails or you can't find a fix.

      Unpacked upstream sources are in {{unpacked_sources}}.
    """

BACKPORT_FIX_BUILD_ERROR_PROMPT_ZSTREAM = BACKPORT_FIX_BUILD_ERROR_PROMPT


async def get_fix_build_error_prompt(fix_version: str | None = None) -> str:
    if fix_version and await is_older_zstream(fix_version):
        return BACKPORT_FIX_BUILD_ERROR_PROMPT_ZSTREAM
    return BACKPORT_FIX_BUILD_ERROR_PROMPT


async def create_backport_agent(
    mcp_tools: list[Tool],
    local_tool_options: dict[str, Any],
    include_build_tools: bool = False,
    fix_version: str | None = None,
) -> ReasoningAgent:
    """
    Create a backport agent.

    Args:
        mcp_tools: List of MCP gateway tools
        local_tool_options: Options for local tools
        include_build_tools: If True, include build_package and download_artifacts tools
                           for iterative build testing during error fixing
        fix_version: Fix version string for z-stream instruction selection
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
        DistgitDetectorTool(options=local_tool_options),
        # Upstream cherry-pick workflow tools
        GetPackageInfoTool(options=local_tool_options),
        ExtractUpstreamRepositoryTool(options=local_tool_options),
        CloneUpstreamRepositoryTool(options=local_tool_options),
        FindBaseCommitTool(options=local_tool_options),
        ApplyDownstreamPatchesTool(options=local_tool_options),
        CherryPickCommitTool(options=local_tool_options),
        CherryPickContinueTool(options=local_tool_options),
    ]

    base_tools.extend([t for t in mcp_tools if t.name == "get_maintainer_rules"])

    # Add clone_repository from MCP gateway (needed for dist-git workflow with auth)
    if fix_version and await is_older_zstream(fix_version):
        base_tools.extend([t for t in mcp_tools if t.name == "clone_repository"])

    # Add build tools if requested (for iterative build error fixing)
    if include_build_tools:
        base_tools.extend([t for t in mcp_tools if t.name in ["build_package", "download_artifacts"]])

    return ReasoningAgent(
        name="BackportAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=base_tools,
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat Enterprise Linux developer",
        instructions=await get_instructions(fix_version),
    )


def _move_build_logs(source_dir: Path, target_dir: Path) -> None:
    """Move build log files from source_dir into target_dir."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for log_file in itertools.chain(
        source_dir.glob("*.log"),
        source_dir.glob("*.log.gz"),
    ):
        log_file.rename(target_dir / log_file.name)


def _update_fix_attempts_log(log_dir: Path, attempt_num: int, build_error: str) -> None:
    """Create or append to fix-attempts.md with the current build error."""
    attempts_log = log_dir / "fix-attempts.md"
    if not attempts_log.exists():
        attempts_log.write_text(
            f"# Fix Attempts Log\n\n"
            f"## Initial build failure\n\n```\n{build_error}\n```\n\n"
            f"## Attempt {attempt_num}\n\n"
            f"**Build error to fix:**\n```\n{build_error}\n```\n\n"
        )
    else:
        with attempts_log.open("a") as f:
            f.write(f"\n## Attempt {attempt_num}\n\n**Build error to fix:**\n```\n{build_error}\n```\n\n")


def _extract_commit_hash(url: str) -> str | None:
    """Extract a commit hash from a dist-git commit URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    match = re.search(r"(?:commit(?:s)?|c)/([a-f0-9]{7,40})", parsed.path)
    if match:
        return match.group(1)
    query_match = re.search(r"(?:id|h)=([a-f0-9]{7,40})", parsed.query or "")
    if query_match:
        return query_match.group(1)
    return None


async def extract_source_changelog(
    local_clone: Path, upstream_patches: list[str], package: str
) -> str | None:
    """Extract changelog messages from source dist-git commits.

    Iterates all upstream patch URLs, extracts the newest changelog entry
    from each commit's spec file, and combines the lines (deduplicating
    across commits). The content is passed through as-is; the LogAgent
    handles replacing Jira references.
    """
    upstream_clone = Path(f"{local_clone}-upstream")
    if not upstream_clone.exists():
        return None

    collected_lines: list[str] = []
    seen: set[str] = set()

    for url in upstream_patches:
        commit_hash = _extract_commit_hash(url)
        if not commit_hash:
            continue

        try:
            stdout, _ = await check_subprocess(
                ["git", "-C", str(upstream_clone), "show", f"{commit_hash}:{package}.spec"],
            )
        except Exception:
            logger.debug(f"Could not read spec from {commit_hash} in {upstream_clone}")
            continue

        try:
            spec = Specfile(content=stdout, sourcedir=upstream_clone)
            with spec.changelog() as changelog:
                if not changelog:
                    continue
                entry = changelog[-1]
        except Exception:
            logger.debug(f"Could not parse spec from {commit_hash}")
            continue

        for line in entry.content:
            if line not in seen:
                seen.add(line)
                collected_lines.append(line)

    if not collected_lines:
        return None

    return "\n".join(collected_lines)


class BackportState(PackageUpdateState):
    upstream_patches: list[str]
    cve_id: str | None
    justification: str | None = Field(default=None)
    unpacked_sources: Path | None = Field(default=None)
    backport_log: list[str] = Field(default_factory=list)
    backport_result: BackportOutputSchema | None = Field(default=None)
    attempts_remaining: int = Field(default=10)
    used_cherry_pick_workflow: bool = Field(default=False)
    incremental_fix_attempts: int = Field(default=0)
    fix_version: str | None = Field(default=None)


async def run_workflow(
    package,
    dist_git_branch,
    upstream_patches,
    jira_issue,
    cve_id,
    justification=None,
    fix_version=None,
    redis_conn=None,
    dry_run=False,
    backport_agent_factory=None,
    max_build_attempts=10,
    max_incremental_fix_attempts=None,
):
    if max_incremental_fix_attempts is None:
        max_incremental_fix_attempts = max_build_attempts

    local_tool_options = {"working_directory": None}
    # In tests SILENT_RUN is typically unset, so Jira status updates are
    # attempted (and skipped via dry_run).  Set SILENT_RUN=true to suppress
    # Jira transitions even when dry_run is False.
    silent_run = os.getenv("SILENT_RUN", "false").lower() == "true"

    async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
        if backport_agent_factory:
            result = backport_agent_factory(gateway_tools, local_tool_options)
            backport_agent = await result if asyncio.iscoroutine(result) else result
        else:
            backport_agent = await create_backport_agent(
                gateway_tools, local_tool_options, fix_version=fix_version
            )
        log_agent = create_log_agent(gateway_tools, local_tool_options)

        workflow = Workflow(BackportState, name="BackportWorkflow")

        async def change_jira_status(state):
            if not dry_run and not silent_run:
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
            state.used_cherry_pick_workflow = False
            state.incremental_fix_attempts = 0

            (
                state.local_clone,
                state.update_branch,
                state.fork_url,
                _,
            ) = await tasks.fork_and_prepare_dist_git(
                jira_issue=state.jira_issue,
                package=state.package,
                dist_git_branch=state.dist_git_branch,
                available_tools=gateway_tools,
            )
            local_tool_options["working_directory"] = state.local_clone
            await run_tool(
                "download_sources",
                dist_git_path=str(state.local_clone),
                package=state.package,
                dist_git_branch=state.dist_git_branch,
                available_tools=gateway_tools,
            )
            if is_cs_branch(state.dist_git_branch):
                pkg_cmd = [
                    "centpkg",
                    f"--name={state.package}",
                    "--namespace=rpms",
                    f"--release={state.dist_git_branch}",
                ]
            else:
                pkg_cmd = [
                    "rhpkg",
                    f"--name={state.package}",
                    "--namespace=rpms",
                    f"--release={state.dist_git_branch}",
                    "--offline",
                    "--released",
                ]
            await check_subprocess([*pkg_cmd, "prep"], cwd=state.local_clone)
            state.unpacked_sources = tasks.get_unpacked_sources(state.local_clone, state.package)
            for idx, upstream_patch in enumerate(state.upstream_patches):
                patch_name = f"{state.jira_issue}-{idx}.patch"
                content = await run_tool(
                    "get_patch_from_url",
                    available_tools=gateway_tools,
                    patch_url=upstream_patch,
                )
                (state.local_clone / patch_name).write_text(content)
            return "run_backport_agent"

        async def run_backport_agent(state):
            pkg_tool = "centpkg" if is_cs_branch(state.dist_git_branch) else "rhpkg --offline --released"
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
                        pkg_tool=pkg_tool,
                    ),
                ),
                expected_output=BackportOutputSchema,
                **get_agent_execution_config(),
            )
            state.backport_result = BackportOutputSchema.model_validate_json(response.last_message.text)
            if state.backport_result.success:
                state.backport_log.append(state.backport_result.status)

                upstream_repo = Path(f"{state.local_clone}-upstream")
                if upstream_repo.exists():
                    try:
                        stdout, _ = await check_subprocess(
                            [
                                "git",
                                "-C",
                                str(upstream_repo),
                                "rev-list",
                                "--count",
                                "HEAD",
                            ]
                        )
                        commit_count = int(stdout.strip())
                        if commit_count > 1:
                            state.used_cherry_pick_workflow = True
                            logger.info(
                                f"Cherry-pick workflow detected: {commit_count} commits in upstream repo"
                            )
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
            return "comment_in_jira"

        async def fix_build_error(state):
            """Try to fix build errors by finding and cherry-picking prerequisite commits."""
            logger.info(
                f"Attempting incremental fix for cherry-pick workflow "
                f"(attempt {state.incremental_fix_attempts}/{max_incremental_fix_attempts})"
            )

            try:
                upstream_repo = Path(f"{state.local_clone}-upstream")
                if not upstream_repo.exists():
                    logger.error(
                        f"Upstream repo {upstream_repo} missing, cannot do incremental fix — "
                        "falling back to full reset"
                    )
                    return "fork_and_prepare_dist_git"

                log_dir = upstream_repo / "build-logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                attempt_num = state.incremental_fix_attempts + 1

                if state.incremental_fix_attempts > 0:
                    _move_build_logs(
                        state.local_clone,
                        log_dir / f"attempt-{state.incremental_fix_attempts}",
                    )
                _update_fix_attempts_log(log_dir, attempt_num, state.build_error)

                fix_agent = await create_backport_agent(
                    gateway_tools,
                    local_tool_options,
                    include_build_tools=True,
                    fix_version=state.fix_version,
                )

                pkg_tool = "centpkg" if is_cs_branch(state.dist_git_branch) else "rhpkg --offline --released"
                response = await fix_agent.run(
                    render_prompt(
                        template=await get_fix_build_error_prompt(fix_version=state.fix_version),
                        input=BackportInputSchema(
                            local_clone=state.local_clone,
                            unpacked_sources=state.unpacked_sources,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                            cve_id=state.cve_id,
                            upstream_patches=state.upstream_patches,
                            build_error=state.build_error,
                            pkg_tool=pkg_tool,
                        ),
                    ),
                    expected_output=BackportOutputSchema,
                    **get_agent_execution_config(),
                )

                fix_result = BackportOutputSchema.model_validate_json(response.last_message.text)

                if fix_result.success:
                    state.backport_result = fix_result
                    state.backport_log.append(fix_result.status)
                    logger.info("Incremental fix succeeded with passing build")
                    state.incremental_fix_attempts = 0
                    return "update_release"

                logger.info(f"Build still failing after fix attempt: {fix_result.error}")
                state.build_error = fix_result.error
                state.backport_result = fix_result

                state.incremental_fix_attempts += 1
                if state.incremental_fix_attempts < max_incremental_fix_attempts:
                    logger.info(
                        f"Will retry incremental fix "
                        f"(attempt {state.incremental_fix_attempts + 1}/{max_incremental_fix_attempts})"
                    )
                    return "fix_build_error"
                logger.error(
                    f"Exhausted all {max_incremental_fix_attempts} incremental fix attempts, giving up"
                )
                state.backport_result.success = False
                state.backport_result.error = (
                    f"Unable to fix build errors after "
                    f"{max_incremental_fix_attempts} incremental fix attempts. "
                    f"Last error: {fix_result.error}"
                )
                return "comment_in_jira"

            except Exception as e:
                logger.error(f"Exception during incremental fix: {e}", exc_info=True)
                state.backport_result.success = False
                state.backport_result.error = f"Exception during incremental fix: {e!s}"
                return "comment_in_jira"

        async def run_build_agent(state):
            if not state.backport_result or not state.backport_result.srpm_path:
                logger.error("Cannot run build agent: no valid backport result or SRPM path")
                state.backport_result = state.backport_result or BackportOutputSchema(
                    success=False,
                    srpm_path=None,
                    status="",
                    error="No SRPM generated by backport agent",
                )
                return "comment_in_jira"

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
            if state.used_cherry_pick_workflow:
                upstream_repo = Path(f"{state.local_clone}-upstream")
                if upstream_repo.exists():
                    _move_build_logs(
                        state.local_clone,
                        upstream_repo / "build-logs" / "attempt-0",
                    )
                logger.info("Cherry-pick workflow was used - starting incremental fix")
                return "fix_build_error"
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
                spec_path = state.local_clone / f"{state.package}.spec"
                with Specfile(spec_path) as spec, spec.patches() as patches:
                    patch_files = [p.expanded_location for p in patches if p.expanded_location]

                if not patch_files:
                    raise RuntimeError(f"Backport completed but no Patch tags found in {spec_path}")

                files_to_git_add = [f"{state.package}.spec", *patch_files]
                logger.info(f"Staging files: {files_to_git_add}")

                await tasks.stage_changes(
                    local_clone=state.local_clone,
                    files_to_commit=files_to_git_add,
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
            source_changelog = await extract_source_changelog(
                state.local_clone, state.upstream_patches, state.package
            )
            if source_changelog:
                logger.info(f"Extracted source changelog for reuse: {source_changelog}")

            response = await log_agent.run(
                render_prompt(
                    template=get_log_prompt(),
                    input=LogInputSchema(
                        jira_issue=state.jira_issue,
                        changes_summary=state.backport_log[-1],
                        source_changelog=source_changelog,
                    ),
                ),
                expected_output=LogOutputSchema,
                **get_agent_execution_config(),
            )
            log_output = LogOutputSchema.model_validate_json(response.last_message.text)

            if redis_conn and not dry_run:
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
                justification_text = format_mr_justification(state.justification)
                (
                    state.merge_request_url,
                    state.merge_request_newly_created,
                ) = await tasks.commit_push_and_open_mr(
                    local_clone=state.local_clone,
                    commit_message=(
                        f"{state.log_result.title}\n\n"
                        f"{state.log_result.description}\n\n"
                        + (f"CVE: {state.cve_id}\n" if state.cve_id else "")
                        + "Upstream patches:\n"
                        + formatted_patches
                        + "\n"
                        + f"Resolves: {state.jira_issue}\n\n"
                        f"This commit was backported {I_AM_YMIR}\n\n"
                        "Assisted-by: Ymir\n"
                    ),
                    fork_url=state.fork_url,
                    dist_git_branch=state.dist_git_branch,
                    update_branch=state.update_branch,
                    mr_title=state.log_result.title,
                    mr_description=(
                        f"{state.log_result.description}\n\n"
                        f"Upstream patches:\n{formatted_patches}\n\n"
                        f"{justification_text}"
                        f"Resolves: {state.jira_issue}\n\n"
                        f"Backporting steps:\n\n{state.backport_log[-1]}"
                        f"\n\n{MR_DESCRIPTION_FOOTER}"
                    ),
                    available_tools=gateway_tools,
                    commit_only=dry_run,
                    labels=["ymir_backport"],
                )
            except Exception as e:
                logger.warning(f"Error committing and opening MR: {e}")
                state.merge_request_url = None
                state.backport_result.success = False
                state.backport_result.error = f"Could not commit and open MR: {e}"
            return "add_fusa_label"

        async def add_fusa_label(state):
            return await PackageUpdateStep.add_fusa_label(
                state,
                "comment_in_jira",
                dry_run=dry_run,
                gateway_tools=gateway_tools,
            )

        async def comment_in_jira(state):
            if dry_run:
                return Workflow.END
            if state.backport_result.success:
                comment_text = (
                    state.merge_request_url if state.merge_request_url else state.backport_result.status
                )
                is_error = False
            else:
                comment_text = f"Agent failed to perform a backport: {state.backport_result.error}"
                is_error = True
            logger.info(f"Result to be put in Jira comment: {comment_text}")
            await tasks.comment_in_jira(
                jira_issue=state.jira_issue,
                agent_type="Backport",
                comment_text=comment_text,
                is_error=is_error,
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
        workflow.add_step("add_fusa_label", add_fusa_label)
        workflow.add_step("comment_in_jira", comment_in_jira)

        response = await workflow.run(
            BackportState(
                package=package,
                dist_git_branch=dist_git_branch,
                upstream_patches=upstream_patches,
                jira_issue=jira_issue,
                cve_id=cve_id,
                justification=justification,
                fix_version=fix_version,
                attempts_remaining=max_build_attempts,
            ),
        )
        return response.state


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    resolve_chat_model_override("backport")

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    max_build_attempts = int(os.getenv("MAX_BUILD_ATTEMPTS", "10"))
    max_incremental_fix_attempts = int(os.getenv("MAX_INCREMENTAL_FIX_ATTEMPTS", str(max_build_attempts)))

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
            justification=os.getenv("JUSTIFICATION", None),
            fix_version=branch,
            redis_conn=None,
            dry_run=dry_run,
            max_build_attempts=max_build_attempts,
            max_incremental_fix_attempts=max_incremental_fix_attempts,
        )
        logger.info(f"Direct run completed: {state.backport_result.model_dump_json(indent=4)}")
        return

    logger.info("Starting backport agent in queue mode")
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        # Determine which backport queue to listen to based on container version
        container_version = os.getenv("CONTAINER_VERSION", "c10s")
        backport_queue = (
            RedisQueues.BACKPORT_QUEUE_C9S.value
            if container_version == "c9s"
            else RedisQueues.BACKPORT_QUEUE_C10S.value
        )
        logger.info(
            f"Connected to Redis, max retries set to {max_retries}, listening to queue: {backport_queue}"
        )

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

            async def retry(task, error, backport_data=backport_data):
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
                        labels_to_remove=[JiraLabels.TRIAGED_BACKPORT.value],
                        dry_run=dry_run,
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
                    justification=backport_data.justification,
                    fix_version=backport_data.fix_version,
                    redis_conn=redis,
                    dry_run=dry_run,
                    max_build_attempts=max_build_attempts,
                    max_incremental_fix_attempts=max_incremental_fix_attempts,
                )
                logger.info(
                    f"Backport processing completed for {backport_data.jira_issue}, "
                    f"success: {state.backport_result.success}"
                )

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=backport_data.jira_issue).model_dump_json(),
                )
            else:
                if state.backport_result.success:
                    logger.info(
                        f"Backport successful for {backport_data.jira_issue}, adding to completed list"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORTED.value],
                        labels_to_remove=[
                            JiraLabels.TRIAGED_BACKPORT.value,
                            JiraLabels.BACKPORT_ERRORED.value,
                            JiraLabels.BACKPORT_FAILED.value,
                        ],
                        dry_run=dry_run,
                    )
                    await fix_await(
                        redis.lpush(
                            RedisQueues.COMPLETED_BACKPORT_LIST.value,
                            state.backport_result.model_dump_json(),
                        )
                    )
                else:
                    logger.warning(
                        f"Backport failed for {backport_data.jira_issue}: {state.backport_result.error}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORT_FAILED.value],
                        labels_to_remove=[JiraLabels.TRIAGED_BACKPORT.value],
                        dry_run=dry_run,
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
