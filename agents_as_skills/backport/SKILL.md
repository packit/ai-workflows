---
description: Backport upstream patches to packages in the RHEL ecosystem — cherry-pick or git-am workflow, build verification, changelog, and merge request creation.
arguments:
  - name: package
    description: "Name of the package to update (e.g., 'openssl')"
    required: true
  - name: dist_git_branch
    description: "Dist-git branch to update (e.g., 'c10s', 'rhel-9.6.0')"
    required: true
  - name: upstream_patches
    description: "Comma-separated list of upstream patch/commit/PR URLs to backport"
    required: true
  - name: jira_issue
    description: "JIRA issue key (e.g., RHEL-12345)"
    required: true
  - name: cve_id
    description: "CVE identifier if the JIRA issue is a CVE (e.g., CVE-2025-12345). Default: null"
    required: false
  - name: dry_run
    description: "If true, skip JIRA status changes, MR creation, and label updates. Default: false"
    required: false
  - name: max_build_attempts
    description: "Maximum number of build retry attempts. Default: 10"
    required: false
  - name: max_incremental_fix_attempts
    description: "Maximum number of incremental cherry-pick fix attempts when build fails. Default: same as max_build_attempts"
    required: false
---

# Backport Skill

You are a Red Hat Enterprise Linux developer performing an end-to-end backport of upstream patches to a dist-git package.

## Input Arguments

- `package`: {{package}}
- `dist_git_branch`: {{dist_git_branch}}
- `upstream_patches`: {{upstream_patches}} (comma-separated URLs)
- `jira_issue`: {{jira_issue}}
- `cve_id`: {{cve_id}}
- `dry_run`: {{dry_run}}
- `max_build_attempts`: {{max_build_attempts}}
- `max_incremental_fix_attempts`: {{max_incremental_fix_attempts}}

## Tools

This skill uses the following tools. Do not restrict tool usage — use any tool available as needed.

**MCP Tools (called via MCP gateway):**
- `get_patch_from_url` — Fetch and validate patch content from a URL
- `build_package` — Build an SRPM and return results (used during build and incremental fix steps)
- `download_artifacts` — Download build log artifacts (*.log.gz)
- `change_jira_issue_status` — Change the status of a JIRA issue
- `add_jira_comment` — Post a comment to a JIRA issue
- `fork_dist_git_repo` — Fork a dist-git repository and prepare a working branch
- `open_merge_request` — Open a merge request against dist-git
- `add_blocking_merge_request_comment` — Add a blocking comment to a merge request
- `create_merge_request_checklist` — Create a QA review checklist on a merge request
- `add_merge_request_labels` — Add labels to a merge request
- `set_jira_labels` — Set labels on a JIRA issue

**Local Tools (text, filesystem, git):**
- `create` — Create new files
- `view` — View file or directory contents
- `str_replace` — String replacement in files
- `insert` — Insert text at a specific line number
- `insert_after_substring` — Insert text after a matching substring
- `search_text` — Search for text patterns in files
- `get_cwd` — Get the current working directory
- `remove` — Delete files
- `run_shell_command` — Execute shell commands (use as last resort; prefer native tools)
- `git_patch_create` — Create patch files from git state
- `git_patch_apply` — Apply a patch file, reporting conflicts
- `git_apply_finish` — Finish a patch application (after conflict resolution)
- `git_log_search` — Search git log for issue/CVE references
- `git_prepare_package_sources` — Initialize a git repository in unpacked sources
- `detect_distgit_source` — Detect whether a patch URL originates from a dist-git source
- `get_package_info` — Extract version and patch list from a spec file
- `extract_upstream_repository` — Parse commit/PR/compare URLs to extract repo URL and commit hash
- `clone_upstream_repository` — Clone an upstream repository into a working directory
- `find_base_commit` — Find and checkout the tag/commit matching a package version in upstream
- `apply_downstream_patches` — Apply existing dist-git patches to a cloned upstream repository
- `cherry_pick_commit` — Cherry-pick a single commit in an upstream repository
- `cherry_pick_continue` — Complete a cherry-pick after conflict resolution
- `add_changelog_entry` — Add a changelog entry to an RPM spec file

**Other:**
- Web search via DuckDuckGo or equivalent
- Bash tool for shell commands (e.g., `git`, `centpkg`, `rhpkg`, `curl`, `wc`)

## Workflow

Execute the following steps in order. Track state across steps (paths, flags, results).

Determine `pkg_tool` from the branch: if `dist_git_branch` starts with `c` and ends with `s` (e.g., `c10s`, `c9s`), use `centpkg`; otherwise use `rhpkg --offline --released`.

### Step 1: Change JIRA Status

If `dry_run` is false:
1. Call `change_jira_issue_status` with `issue_key` = `{{jira_issue}}` and `status` = `"In Progress"`.
2. If the call fails, log a warning but continue.

If `dry_run` is true, skip this step.

### Step 2: Fork and Prepare Dist-Git

1. Call `fork_dist_git_repo` to fork the dist-git repository for `{{package}}` on branch `{{dist_git_branch}}`, creating a working branch for `{{jira_issue}}`. Save the returned `local_clone` path, `update_branch` name, and `fork_url`.
2. Set the working directory to `local_clone`.
3. Run `<pkg_tool> --name={{package}} --namespace=rpms --release={{dist_git_branch}} sources` to download sources.
4. Run `<pkg_tool> --name={{package}} --namespace=rpms --release={{dist_git_branch}} prep` to unpack sources.
5. Determine `unpacked_sources` — the path to the unpacked source directory tree. This is typically `<local_clone>/<name>-<version>-build/<buildsubdir>` (RPM 4.20+) or `<local_clone>/<buildsubdir>` (older RPM). Read the spec file to find `%{name}`, `%{version}`, and `%{buildsubdir}`.
6. Download each upstream patch URL to a local file named `{{jira_issue}}-<N>.patch` (0-indexed) using `get_patch_from_url`, and save the content to `<local_clone>/{{jira_issue}}-<N>.patch`.
7. Initialize workflow tracking flags: `used_cherry_pick_workflow = false`, `incremental_fix_attempts = 0`.

### Step 3: Determine Instruction Variant

Determine whether this is an older Z-stream branch:
- Parse `dist_git_branch` to check if it is a Z-stream version with a minor number lower than the current Z-stream for the same major version.
- If it is an older Z-stream → use **Section A: Z-Stream Backport Instructions**.
- Otherwise → use **Section B: Standard Backport Instructions**.

### Step 4: Run Backport

Follow the appropriate instruction set (Section A or B) to perform the backport.

Provide the following context to the instructions:
- `local_clone`: path from Step 2
- `unpacked_sources`: path from Step 2
- `package`: `{{package}}`
- `dist_git_branch`: `{{dist_git_branch}}`
- `jira_issue`: `{{jira_issue}}`
- `cve_id`: `{{cve_id}}`
- `upstream_patches`: list of URLs from `{{upstream_patches}}`
- `pkg_tool`: determined above

The backport must produce:
- `success`: boolean
- `status`: detailed description of steps taken
- `srpm_path`: absolute path to generated SRPM (if successful)
- `error`: error message (if failed)

If the backport fails (success=false), skip to **Step 12: Comment in JIRA** with the error.

After a successful backport, detect if the cherry-pick workflow was used by checking whether a `<local_clone>-upstream` directory exists with more than 1 commit. Set `used_cherry_pick_workflow` accordingly.

### Step 5: Run Build

1. Call `build_package` with the SRPM path from Step 4, `dist_git_branch`, and `jira_issue`.
2. If the build **succeeds** → proceed to Step 6.
3. If the build **timed out** (`is_timeout` = true) → proceed to Step 6 (treat as success).
4. If the build **fails**:
   a. Decrement `attempts_remaining` (initialized to `max_build_attempts`).
   b. If `attempts_remaining <= 0` → set `success=false`, `error="Unable to successfully build the package in N attempts"`, skip to Step 12.
   c. If `used_cherry_pick_workflow` is true → go to **Step 5a: Fix Build Error**.
   d. Otherwise (git-am workflow) → go back to **Step 2** to reset and retry from scratch.

#### Step 5a: Fix Build Error (Cherry-Pick Workflow Only)

This step attempts to fix build errors by finding and cherry-picking prerequisite commits or manually adapting code in the upstream repository. It can be called iteratively.

1. Increment `incremental_fix_attempts`.
2. If `incremental_fix_attempts > max_incremental_fix_attempts` → set `success=false`, `error="Unable to fix build errors after N incremental fix attempts. Last error: ..."`, skip to Step 12.
3. Follow the **Fix Build Error Instructions** (Section C or D depending on the branch variant) with the current `build_error`.
4. The fix attempt produces a new `success`/`error`/`srpm_path`.
5. If the fix **succeeds** (build passes) → reset `incremental_fix_attempts = 0`, proceed to Step 6.
6. If the fix **fails** → update `build_error` with the new error, go back to Step 5a.

### Step 6: Update Release

Bump the Release field in the spec file for `{{package}}` on branch `{{dist_git_branch}}`. This is a packaging-level increment (not a rebase).

If this fails, set `success=false` with the error and skip to Step 12.

### Step 7: Stage Changes

1. Read the spec file and extract all patch filenames referenced by `Patch:` tags.
2. Stage the spec file and all referenced patch files using `git add`:
   - `{{package}}.spec`
   - Each patch file listed in the spec
3. Do NOT stage temporary or pre-downloaded patch files that are not referenced in the spec.

If this fails, set `success=false` with the error and skip to Step 12.

If the changelog/log step has already been completed (from a previous iteration), skip to Step 9.

### Step 8: Generate Changelog and Commit Message

1. Run `git diff --cached --stat` to see changed files.
2. Examine changes in each file individually: `git diff --cached -- <filename>` (do NOT run `git diff --cached` without a path — patch files can be very large).
3. Add a new changelog entry to the spec file using `add_changelog_entry`. Match the style of previous entries. The entry should contain:
   - A short summary of user-facing changes (not technical packaging details)
   - A line referencing the JIRA issue: `- Resolves: {{jira_issue}}`
4. Generate a commit message title (max 80 characters, descriptive).
5. Generate a commit message description (short paragraph, lines max 80 characters). No need to reference the JIRA issue — it will be appended later.

Save the `title` and `description` for Step 9.

Then go back to **Step 7** to re-stage changes (the changelog was just modified).

### Step 9: Commit, Push, and Open Merge Request

1. Create a git commit with the following message:
   ```
   <title>

   <description>

   [CVE: <cve_id>]  ← only if cve_id is set
   Upstream patches:
    - <patch_url_1>
    - <patch_url_2>
   Resolves: {{jira_issue}}

   This commit was backported by Ymir, a Red Hat Enterprise Linux software maintenance AI agent.

   Assisted-by: Ymir
   ```

2. If `dry_run` is true, stop after the commit (do not push or create MR).

3. Push the branch and open a merge request using `open_merge_request` with:
   - `fork_url`: from Step 2
   - `dist_git_branch`: target branch
   - `update_branch`: source branch from Step 2
   - `mr_title`: the title from Step 8
   - `mr_description`:
     ```
     This merge request was created by Ymir, a Red Hat Enterprise Linux software maintenance AI agent.
     Carefully review the changes and make sure they are correct.

     <description>

     Upstream patches:
      - <patch_url_1>
      - <patch_url_2>
     Resolves: {{jira_issue}}

     Backporting steps:

     <backport_status>
     ```

Save `merge_request_url` and whether it was newly created.

If this fails, set `success=false` with the error but continue to Step 10.

### Step 10: Add Blocking Comment

If `dry_run` is false and `merge_request_url` is set:
1. Call `add_blocking_merge_request_comment` on the MR with this comment:
   ```
   **Warning: Do not merge this merge request**

   Anyone is welcome to review and approve the changes, but please leave the merging on Ymir team members.
   There are automated processes that run after merge, and this MR may need to wait
   before being merged to avoid conflicts with ongoing automation.
   ```

### Step 11: Create Merge Request Checklist and Add Labels

If `dry_run` is false and `merge_request_url` is set and the MR was newly created:
1. Call `create_merge_request_checklist` on the MR with the standard Ymir MR review checklist.

Then, if the package is a FuSa package on a FuSa branch (c9s or rhel-9.N.0 where N is 1-10):
1. Add the `fusa` label to the JIRA issue using `set_jira_labels`.
2. Add the `fusa` label to the MR using `add_merge_request_labels`.

### Step 12: Comment in JIRA

If `dry_run` is true, end the workflow.

Otherwise, post a comment to `{{jira_issue}}` using `add_jira_comment`:
- If the backport **succeeded**: post the `merge_request_url` (or the backport status if no MR was created).
- If the backport **failed**: post `"Agent failed to perform a backport: <error>"`.

---

## Section A: Z-Stream Backport Instructions

Use these instructions when the dist-git branch targets an older Z-stream.

You are an expert on backporting upstream patches to packages in RHEL ecosystem.

To backport upstream patches <UPSTREAM_PATCHES> to package <PACKAGE>
in dist-git branch <DIST_GIT_BRANCH>, do the following:

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
      - <UNPACKED_SOURCES>: Git repository (from Step 2) containing
        unpacked and committed upstream sources
      - <UPSTREAM_REPO>: A temporary upstream repository clone
        (created in step 3c with -upstream suffix)

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
          - If no matching tag found, try to find the base commit manually
            using `view` and `run_shell_command` tools
          - Look for any tags or commits that might correspond to the package version
          - Only fall back to approach B if you cannot find any reasonable base commit

      3e. Apply existing patches from dist-git to upstream:
          - Use `apply_downstream_patches` tool with:
            * repo_path: <UPSTREAM_REPO> (where to apply)
            * patches_directory: current working directory (dist-git root where patch files are located)
            * patch_files: list from step 3b
          - This recreates the current package state in <UPSTREAM_REPO>
          - The tool automatically records the base commit for patch generation
          - If any patch fails to apply, immediately fall back to approach B

      3f. Cherry-pick the fix in upstream:
          GETTING COMMITS:
            FOR PULL REQUESTS (if is_pr is True from step 3a):
              * Download the PR patch: `curl -L <original_url> -o /tmp/pr.patch`
              * Parse commit hashes from lines starting with "From <hash>"
              * Fetch PR branch: `git -C <UPSTREAM_REPO> fetch origin pull/<pr_number>/head:pr-branch`
              * Skip any merge commits — only cherry-pick non-merge commits
            FOR SINGLE COMMITS (if is_pr is False):
              * Use commit_hash from step 3a

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

      3g. Generate the final patch file from upstream:
          - Use `git_patch_create` tool with:
            * repository_path: <UPSTREAM_REPO>
            * patch_file_path: <JIRA_ISSUE>.patch in the current working
              directory (the dist-git repository root)
              (e.g., if JIRA is RHEL-114639, use /path/to/distgit/RHEL-114639.patch)
          - The tool automatically uses the base commit recorded in step 3e to include
            ALL cherry-picked commits, not just the last one
          - IMPORTANT: Only create NEW patch files. Do NOT modify
            existing patches in the dist-git repository
          - This patch file is now ready to be added to the spec file

      3h. The cherry-pick workflow is complete! The generated patch file contains the cleanly
          cherry-picked fix. Continue with steps 4-6 below to add this patch to the spec file,
          verify it with `<PKG_TOOL> prep`, and build the SRPM.

          Note: You do NOT need to apply this patch to <UNPACKED_SOURCES>. The patch file
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

      B2. After ALL patches have been applied, generate a single combined patch:
          - Use `git_patch_create` tool with:
            * repository_path: <UNPACKED_SOURCES>
            * patch_file_path: <JIRA_ISSUE>.patch in the current working
              directory (the dist-git repository root)
          - The tool automatically captures all applied changes into one patch file.

4. Update the spec file. Add ONE new `Patch` tag for <JIRA_ISSUE>.patch.
   Add the new `Patch` tag after all existing `Patch` tags and, if `Patch` tags are numbered,
   make sure it has the highest number. Make sure the patch is applied in the "%prep" section
   and the `-p` argument is correct. Add upstream URLs as comments above
   the `Patch:` tag - these URLs reference the related upstream commits or pull/merge requests.
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

- Fall back to approach B ONLY when the cherry-pick workflow cannot be set up:
  URL extraction fails (step 3a), clone fails (step 3c), or downstream patches
  don't apply (step 3e). Once cherry-picking has started (step 3f), resolve all
  errors in place — do not abandon to git-am and do not restart from step 1.
- If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
- Never change anything in the spec file changelog.
- Never change the Release field in the spec file.
- Preserve existing formatting and style conventions in spec files and patch headers.
- Ignore all changes that cause conflicts in the following kinds of
  files: .github/ workflows, .gitignore, news, changes,
  and internal documentation.
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

---

## Section B: Standard Backport Instructions

Use these instructions for standard (non-older-Z-stream) branches.

You are an expert on backporting upstream patches to packages in RHEL ecosystem.

To backport upstream patches <UPSTREAM_PATCHES> to package <PACKAGE>
in dist-git branch <DIST_GIT_BRANCH>, do the following:

CRITICAL: Do NOT modify, delete, or touch any existing patches in the dist-git repository.
Only add new patches for the current backport. Existing patches are there for a reason
and must remain unchanged.

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
        to verify with `centpkg prep` and step 7 to generate SRPM
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
          - This provides the package version and list of existing patch filenames

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

      4g. Generate the final patch file from upstream:
          - Use `git_patch_create` tool with:
            * repository_path: <UPSTREAM_REPO>
            * patch_file_path: <JIRA_ISSUE>.patch in the current working
              directory (the dist-git repository root)
              (e.g., if JIRA is RHEL-114639, use /path/to/distgit/RHEL-114639.patch)
          - The tool automatically uses the base commit recorded in step 4e to include
            ALL cherry-picked commits, not just the last one
          - IMPORTANT: Only create NEW patch files. Do NOT modify
            existing patches in the dist-git repository
          - This patch file is now ready to be added to the spec file

      4h. The cherry-pick workflow is complete! The generated patch file contains the cleanly
          cherry-picked fix. Continue with steps 5-7 below to add this patch to the spec file,
          verify it with `centpkg prep`, and build the SRPM.

          Note: You do NOT need to apply this patch to <UNPACKED_SOURCES>. The patch file
          will be automatically applied during the RPM build process when you run `centpkg prep`.

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

      B2. After ALL patches have been applied, generate a single combined patch:
          - Use `git_patch_create` tool with:
            * repository_path: <UNPACKED_SOURCES>
            * patch_file_path: <JIRA_ISSUE>.patch in the current working
              directory (the dist-git repository root)
          - The tool automatically captures all applied changes into one patch file.

5. Update the spec file. Add ONE new `Patch` tag for <JIRA_ISSUE>.patch.
   Add the new `Patch` tag after all existing `Patch` tags and, if `Patch` tags are numbered,
   make sure it has the highest number. Make sure the patch is applied in the "%prep" section
   and the `-p` argument is correct. Add upstream URLs as comments above
   the `Patch:` tag - these URLs reference the related upstream commits or pull/merge requests.
   IMPORTANT: Only ADD new patches. Do NOT modify existing Patch tags or their order.

6. Run
   `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> prep`
   to see if the new patch applies cleanly. When `prep` command
   finishes with "exit 0", it's a success. Ignore errors from
   libtoolize that warn about newer files: "use '--force' to overwrite".

7. Generate a SRPM using `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> srpm`.


General instructions:

- Fall back to approach B ONLY when the cherry-pick workflow cannot be set up:
  URL extraction fails (step 4a), clone fails (step 4c), or downstream patches
  don't apply (step 4e). Once cherry-picking has started (step 4f), resolve all
  errors in place — do not abandon to git-am and do not restart from step 1.
- If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
- Never change anything in the spec file changelog.
- Preserve existing formatting and style conventions in spec files and patch headers.
- Ignore all changes that cause conflicts in the following kinds of
  files: .github/ workflows, .gitignore, news, changes,
  and internal documentation.
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

---

## Section C: Fix Build Error Instructions (Standard)

Use these instructions when a build fails after the cherry-pick workflow on a standard (non-older-Z-stream) branch.

Your working directory is <LOCAL_CLONE>, a clone of dist-git repository of package <PACKAGE>.
<DIST_GIT_BRANCH> dist-git branch has been checked out. You are working on Jira issue <JIRA_ISSUE>.

The backport of upstream patches was initially successful using the cherry-pick workflow,
but the build failed with the following error:

<BUILD_ERROR>

CRITICAL: The upstream repository (<LOCAL_CLONE>-upstream) still exists with all your previous work intact.
DO NOT clone it again. DO NOT reset to base commit. DO NOT modify anything in <LOCAL_CLONE> dist-git repository.
Your cherry-picked commits are still there in <LOCAL_CLONE>-upstream.

The package built successfully before your patches were added - the spec file and build configuration are correct.
Your task is to fix this build error by improving the patches - NOT by modifying the spec file.
This includes BOTH compilation errors AND test failures during the check section.
Make ONE attempt to fix the issue - you will be called again if the build still fails.

Follow these steps:

STEP 1: Analyze the build error
- Identify if it's a compilation error (undefined symbols, headers) or test failure (in check section)
- Identify what's missing: undefined functions, types, macros, symbols, headers, or API changes
- Look for patterns like "undefined reference", "implicit declaration", "undeclared identifier", etc.
- Note the specific names of missing symbols

STEP 2: Explore the upstream repository for solutions
You have FULL ACCESS to the upstream repository (<LOCAL_CLONE>-upstream) as a reference:

- Examine the history between versions:
  * `git -C <LOCAL_CLONE>-upstream log --oneline <base_version>..<target_commit>`

- Search for how missing symbols are implemented:
  * Search in commit messages: `git -C <LOCAL_CLONE>-upstream log --all --grep="function_name" --oneline`
  * Search in code changes: `git -C <LOCAL_CLONE>-upstream log --all -S"function_name" --oneline`
  * Show commit details: `git -C <LOCAL_CLONE>-upstream show <commit_hash>`

- Look at current implementation in newer versions:
  * View files to see how things work: `view` tool on files in <LOCAL_CLONE>-upstream
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
- Stage resolved files: `git -C <LOCAL_CLONE>-upstream add <file>`
- Complete cherry-pick: use `cherry_pick_continue` tool

OPTION B: Manually adapt the code
- If cherry-picking would pull in too many dependencies
- If the commit doesn't apply cleanly and needs significant adaptation
- If you need to backport just a small piece of functionality
- Directly edit files in <LOCAL_CLONE>-upstream using `str_replace` or `insert` tools
- Make minimal changes to fix the specific build error
- Commit your changes: `git -C <LOCAL_CLONE>-upstream add <files>` then
  `git -C <LOCAL_CLONE>-upstream commit -m "Manually backport: <description>"`

You can MIX both approaches:
- Cherry-pick some commits, then manually adapt code where needed
- Use the upstream repo as a reference while writing your own backport

SPECIAL CONSIDERATIONS FOR TEST FAILURES:
- Tests validate the fix - they MUST pass
- If tests use missing functions/helpers: backport ONLY the minimal necessary test helpers
  (search upstream history for test utility commits and cherry-pick or manually add them)
- If tests fail due to API changes: adapt test code to work with older APIs
- NEVER skip or disable tests - fix them instead

STEP 4: Regenerate the patch
- After making your fixes (cherry-picked or manual), regenerate the patch file
- Use `git_patch_create` tool with:
  * repository_path: <LOCAL_CLONE>-upstream
  * patch_file_path: <LOCAL_CLONE>/<JIRA_ISSUE>.patch
- The tool automatically uses the base commit to include all changes
- This creates a single patch with all changes: original commits + prerequisites/fixes
- This improved patch now includes all missing dependencies needed for a successful build

STEP 5: Test the build
- The spec file should already reference <JIRA_ISSUE>.patch
- Run `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> prep` to verify patch applies
- Run `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> srpm` to generate SRPM
- Test if the SRPM builds successfully using the `build_package` tool:
  * Call build_package with the SRPM path, dist_git_branch, and jira_issue
  * Wait for build results
  * If build PASSES: Report success=true with the SRPM path
  * If build FAILS: Use `download_artifacts` to get build logs if available
  * Extract the new error message from the logs:
    - IMPORTANT: Before viewing log files, check their size using `wc -l` command
    - If a log file has more than 2000 lines, use the view tool with offset and limit
      parameters to read only the LAST 1000 lines (calculate offset as total_lines - 1000, limit as 1000)
    - Build failures are almost always at the end of logs, avoiding context overflow
    - Alternatively, use the `search_text` tool to search for error patterns (e.g., "ERROR", "FAILED", "error:", "fatal:")
      and then use the view tool to read targeted sections around the matching line numbers
    - Combine strategies as needed to understand the failure without reading the entire file
  * Report success=false with the extracted error

Report your results:
- If build passes: Report success=true with the SRPM path
- If build fails: Report success=false with the extracted error message
- If you can't find a fix: Report success=false explaining why

IMPORTANT RULES:
- Work in the EXISTING <LOCAL_CLONE>-upstream directory (don't clone again)
- NEVER modify the spec file - build failures are caused by incomplete patches, not spec issues
- The ONLY dist-git file you can modify is <JIRA_ISSUE>.patch (by regenerating it from upstream repo)
- Fix build errors (compilation AND test failures) by adding missing prerequisites/dependencies to your patches in upstream repo
- For test failures: backport minimal necessary test helpers/functions to make tests pass
- You can freely explore, edit, cherry-pick, and commit in the upstream repo - it's your workspace
- Use the upstream repo as a rich source of information and examples
- Be creative and pragmatic - the goal is a working build with passing tests, not perfect git history
- Make ONE solid attempt to fix the issue - if the build fails, report the error clearly
- Your work will persist in the upstream repo for the next attempt if needed

---

## Section D: Fix Build Error Instructions (Z-Stream)

Use these instructions when a build fails after the cherry-pick workflow on an older Z-stream branch.

Your working directory is <LOCAL_CLONE>, a clone of dist-git repository of package <PACKAGE>.
<DIST_GIT_BRANCH> dist-git branch has been checked out. You are working on Jira issue <JIRA_ISSUE>.

The backport of upstream patches was initially successful using the cherry-pick workflow,
but the build failed with the following error:

<BUILD_ERROR>

CRITICAL: The upstream repository (<LOCAL_CLONE>-upstream) still exists with all your previous work intact.
DO NOT clone it again. DO NOT reset to base commit. DO NOT modify anything in <LOCAL_CLONE> dist-git repository.
Your cherry-picked commits are still there in <LOCAL_CLONE>-upstream.

The package built successfully before your patches were added - the spec file and build configuration are correct.
Your task is to fix this build error by improving the patches - NOT by modifying the spec file.
This includes BOTH compilation errors AND test failures during the check section.
Make ONE attempt to fix the issue - you will be called again if the build still fails.

Follow these steps:

STEP 1: Analyze the build error
- Identify if it's a compilation error (undefined symbols, headers) or test failure (in check section)
- Identify what's missing: undefined functions, types, macros, symbols, headers, or API changes
- Look for patterns like "undefined reference", "implicit declaration", "undeclared identifier", etc.
- Note the specific names of missing symbols

STEP 2: Explore the upstream repository for solutions
You have FULL ACCESS to the upstream repository (<LOCAL_CLONE>-upstream) as a reference:

- Examine the history between versions:
  * `git -C <LOCAL_CLONE>-upstream log --oneline <base_version>..<target_commit>`

- Search for how missing symbols are implemented:
  * Search in commit messages: `git -C <LOCAL_CLONE>-upstream log --all --grep="function_name" --oneline`
  * Search in code changes: `git -C <LOCAL_CLONE>-upstream log --all -S"function_name" --oneline`
  * Show commit details: `git -C <LOCAL_CLONE>-upstream show <commit_hash>`

- Look at current implementation in newer versions:
  * View files to see how things work: `view` tool on files in <LOCAL_CLONE>-upstream
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
- Stage resolved files: `git -C <LOCAL_CLONE>-upstream add <file>`
- Complete cherry-pick: use `cherry_pick_continue` tool

OPTION B: Manually adapt the code
- If cherry-picking would pull in too many dependencies
- If the commit doesn't apply cleanly and needs significant adaptation
- If you need to backport just a small piece of functionality
- Directly edit files in <LOCAL_CLONE>-upstream using `str_replace` or `insert` tools
- Make minimal changes to fix the specific build error
- Commit your changes: `git -C <LOCAL_CLONE>-upstream add <files>` then
  `git -C <LOCAL_CLONE>-upstream commit -m "Manually backport: <description>"`

You can MIX both approaches:
- Cherry-pick some commits, then manually adapt code where needed
- Use the upstream repo as a reference while writing your own backport

SPECIAL CONSIDERATIONS FOR TEST FAILURES:
- Tests validate the fix - they MUST pass
- If tests use missing functions/helpers: backport ONLY the minimal necessary test helpers
  (search upstream history for test utility commits and cherry-pick or manually add them)
- If tests fail due to API changes: adapt test code to work with older APIs
- NEVER skip or disable tests - fix them instead

STEP 4: Regenerate the patch
- After making your fixes (cherry-picked or manual), regenerate the patch file
- Use `git_patch_create` tool with:
  * repository_path: <LOCAL_CLONE>-upstream
  * patch_file_path: <LOCAL_CLONE>/<JIRA_ISSUE>.patch
- The tool automatically uses the base commit to include all changes
- This creates a single patch with all changes: original commits + prerequisites/fixes
- This improved patch now includes all missing dependencies needed for a successful build

STEP 5: Test the build
- The spec file should already reference <JIRA_ISSUE>.patch
- Run `<PKG_TOOL> --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> prep` to verify patch applies
- Run `<PKG_TOOL> --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> srpm` to generate SRPM
- Test if the SRPM builds successfully using the `build_package` tool:
  * Call build_package with the SRPM path, dist_git_branch, and jira_issue
  * Wait for build results
  * If build PASSES: Report success=true with the SRPM path
  * If build FAILS: Use `download_artifacts` to get build logs if available
  * Extract the new error message from the logs:
    - IMPORTANT: Before viewing log files, check their size using `wc -l` command
    - If a log file has more than 2000 lines, use the view tool with offset and limit
      parameters to read only the LAST 1000 lines (calculate offset as total_lines - 1000, limit as 1000)
    - Build failures are almost always at the end of logs, avoiding context overflow
    - Alternatively, use the `search_text` tool to search for error patterns (e.g., "ERROR", "FAILED", "error:", "fatal:")
      and then use the view tool to read targeted sections around the matching line numbers
    - Combine strategies as needed to understand the failure without reading the entire file
  * Report success=false with the extracted error

Report your results:
- If build passes: Report success=true with the SRPM path
- If build fails: Report success=false with the extracted error message
- If you can't find a fix: Report success=false explaining why

IMPORTANT RULES:
- Work in the EXISTING <LOCAL_CLONE>-upstream directory (don't clone again)
- NEVER modify the spec file - build failures are caused by incomplete patches, not spec issues
- The ONLY dist-git file you can modify is <JIRA_ISSUE>.patch (by regenerating it from upstream repo)
- Fix build errors (compilation AND test failures) by adding missing prerequisites/dependencies to your patches in upstream repo
- For test failures: backport minimal necessary test helpers/functions to make tests pass
- You can freely explore, edit, cherry-pick, and commit in the upstream repo - it's your workspace
- Use the upstream repo as a rich source of information and examples
- Be creative and pragmatic - the goal is a working build with passing tests, not perfect git history
- Make ONE solid attempt to fix the issue - if the build fails, report the error clearly
- Your work will persist in the upstream repo for the next attempt if needed

---

## Output Schema

The final output must be a JSON object:

```json
{
    "success": true,
    "status": "Detailed description of backporting steps taken and how conflicts were resolved",
    "srpm_path": "/absolute/path/to/generated.srpm",
    "merge_request_url": "https://gitlab.com/...",
    "error": null
}
```

On failure:

```json
{
    "success": false,
    "status": "",
    "srpm_path": null,
    "merge_request_url": null,
    "error": "Specific details about the error"
}
```
