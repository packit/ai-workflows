---
description: Rebuild packages against updated dependencies in the RHEL ecosystem — release bump, changelog, and merge request creation with no source code changes. Supports consolidating multiple sibling Jira issues into a single rebuild MR.
arguments:
  - name: package
    description: "Name of the package to rebuild (e.g., 'openssl')"
    required: true
  - name: dist_git_branch
    description: "Dist-git branch to update (e.g., 'c10s', 'rhel-9.6.0')"
    required: true
  - name: jira_issue
    description: "Primary JIRA issue key (e.g., RHEL-12345)"
    required: true
  - name: dependency_issue
    description: "JIRA issue key of the dependency that was updated (e.g., RHEL-67890). Default: null"
    required: false
  - name: dependency_component
    description: "Name of the dependency component that was updated (e.g., 'golang'). Default: null"
    required: false
  - name: consolidated_issues
    description: "JSON list of sibling issues consolidated into this rebuild. Each item has 'issue_key' and optional 'dependency_component'. Example: [{\"issue_key\": \"RHEL-67890\", \"dependency_component\": \"golang\"}]. Default: []"
    required: false
  - name: consolidation_summary
    description: "Summary text of the sibling consolidation analysis from triage. Default: null"
    required: false
  - name: dry_run
    description: "If true, skip JIRA status changes and MR creation (commit only). Default: false"
    required: false
---

# Rebuild Skill

You are a Red Hat Enterprise Linux developer performing an end-to-end rebuild of a package against updated dependencies. This workflow makes no source code changes — it only bumps the release, adds a changelog entry, and opens a merge request. When consolidated sibling issues are provided, a single rebuild MR resolves all of them.

## Input Arguments

- `package`: {{package}}
- `dist_git_branch`: {{dist_git_branch}}
- `jira_issue`: {{jira_issue}}
- `dependency_issue`: {{dependency_issue}}
- `dependency_component`: {{dependency_component}}
- `consolidated_issues`: {{consolidated_issues}}
- `consolidation_summary`: {{consolidation_summary}}
- `dry_run`: {{dry_run}}

## Derived Values

Compute these at the start:

- `all_jira_issues`: a list starting with `{{jira_issue}}`, followed by the `issue_key` of each item in `consolidated_issues`. Example: `["RHEL-12345", "RHEL-67890", "RHEL-11111"]`.
- `all_jira_issues_str`: the above joined by commas. Example: `"RHEL-12345, RHEL-67890, RHEL-11111"`.
- `all_dependency_components`: the unique set of dependency component names, combining `dependency_component` (if set) with `dependency_component` from each consolidated issue (if set). Sort alphabetically.

## Tools

This skill uses the following tools. Do not restrict tool usage — use any tool available as needed.

**MCP Tools (called via MCP gateway):**
- `fork_repository` — Fork a dist-git repository on GitLab
- `clone_repository` — Clone a Git repository to a local path
- `create_zstream_branch` — Create a z-stream branch for a package (non-CentOS Stream branches only)
- `push_to_remote_repository` — Push a branch to a remote repository
- `open_merge_request` — Open a merge request against dist-git
- `add_jira_comment` — Post a comment to a JIRA issue

**Local Tools (text, filesystem, git):**
- `create` — Create new files
- `view` — View file or directory contents
- `str_replace` — String replacement in files
- `insert` — Insert text at a specific line number
- `insert_after_substring` — Insert text after a matching substring
- `search_text` — Search for text patterns in files
- `get_cwd` — Get the current working directory
- `run_shell_command` — Execute shell commands (use as last resort; prefer native tools)
- `add_changelog_entry` — Add a changelog entry to an RPM spec file
- `update_release` — Bump the Release field in a spec file

**Other:**
- Web search via DuckDuckGo or equivalent
- Bash tool for shell commands (e.g., `git`, `centpkg`, `rhpkg`)

## Workflow

Execute the following steps in order. Track state across steps (paths, flags, results).

### Step 1: Fork and Prepare Dist-Git

1. Determine the namespace from the branch:
   - If `dist_git_branch` starts with `c` and ends with `s` (e.g., `c10s`, `c9s`): namespace is `centos-stream`.
   - Otherwise: namespace is `rhel`.
2. Fork the repository by calling `fork_repository` with `repository` = `https://gitlab.com/redhat/<namespace>/rpms/{{package}}`. Save the returned `fork_url`.
3. If the namespace is `rhel` (not CentOS Stream), call `create_zstream_branch` with `package` = `{{package}}` and `branch` = `{{dist_git_branch}}` to ensure the branch exists.
4. Clone the repository by calling `clone_repository` with the repository URL, `branch` = `{{dist_git_branch}}`, and a local clone path. Save `local_clone`.
5. Create a working branch: `git checkout -B automated-package-update-{{jira_issue}}` in `local_clone`. Save `update_branch` = `automated-package-update-{{jira_issue}}`.
6. Set the working directory to `local_clone`.

### Step 2: Update Release

Bump the Release field in the spec file `{{package}}.spec` for package `{{package}}` on branch `{{dist_git_branch}}`. This is a packaging-level increment (not a rebase).

If this fails, set `rebuild_success=false` with the error and skip to **Step 6: Comment in JIRA**.

### Step 3: Generate Changelog and Commit Message

1. Run `git diff --cached --stat` to see which files have been changed.
2. Examine changes in each file individually: `git diff -- <filename>` (do NOT run `git diff` without a path).
3. Determine the changes summary based on the dependency context:
   - If `all_dependency_components` is non-empty: the summary is `"Rebuild of {{package}} for <all_jira_issues_str> against updated <all_dependency_components joined by comma>. The changelog entry and commit title MUST mention <all_dependency_components joined by comma>."`
   - Otherwise: the summary is `"Rebuild of {{package}} against updated dependencies for <all_jira_issues_str>."`
4. Add a new changelog entry to the spec file using `add_changelog_entry`. Examine the previous changelog entries and try to use the same style. The entry should contain:
   - A short summary of the user-facing changes (not technical packaging details)
   - A line referencing all JIRA issues: `- Resolves: <all_jira_issues_str>` (comma-separated on one line) unless the spec file has historically used a different style.
5. Generate a title for the commit message and merge request. It should be descriptive but no longer than 80 characters.
6. Generate a description as a short paragraph for the commit message and merge request. Line length should not exceed 80 characters. Do NOT include `Resolves:` lines — JIRA references are appended separately.

Save the `title` and `description` for Step 5.

### Step 4: Stage Changes

1. Stage the spec file using `git add --all {{package}}.spec`.

If this fails, set `rebuild_success=false` with the error and skip to **Step 6: Comment in JIRA**.

### Step 5: Commit, Push, and Open Merge Request

1. Check if anything is actually staged by running `git diff --cached --quiet`.
   - Exit code 0 means no staged changes (commit would be empty) — set `allow_empty=true`.
   - Exit code 1 means there are staged changes — set `allow_empty=false`.

2. Construct dependency metadata lines:
   - If `all_dependency_components` has one component: `Dependencies: <component>` (use header "Dependency" for a single component).
   - If `all_dependency_components` has multiple: `Dependencies: <component1>, <component2>, ...`.
   - Only include this line if `all_dependency_components` is non-empty.

3. Construct the resolves line: `Resolves: <all_jira_issues_str>` (all issues comma-separated on one line).

4. Create a git commit with the following message:
   ```
   <title>

   <description>

   [Dependency: <dependency_components>]  ← only if all_dependency_components is non-empty
   Resolves: <all_jira_issues_str>

   This commit was created by Ymir, a Red Hat Enterprise Linux software maintenance AI agent.

   Assisted-by: Ymir
   ```

5. If `dry_run` is true, stop after the commit (do not push or create MR). Set `rebuild_success=true`.

6. Push the branch to the fork using `push_to_remote_repository` with:
   - `repository`: `fork_url`
   - `clone_path`: `local_clone`
   - `branch`: `update_branch`
   - `force`: true

7. Construct the MR description:
   ```
   <description>

   [Dependency: <dependency_components>]  ← only if all_dependency_components is non-empty
   Resolves: <all_jira_issues_str>
   [
   Sibling consolidation analysis:
   <consolidation_summary>
   ]  ← only if consolidation_summary is set


   ---

   > **Warning: AI-Generated MR**: Created by Ymir AI assistant. AI may make mistakes...
   ```

8. Open a merge request using `open_merge_request` with:
   - `fork_url`: from Step 1
   - `target`: `{{dist_git_branch}}`
   - `source`: `update_branch` from Step 1
   - `title`: the title from Step 3
   - `description`: the MR description constructed above

9. Save the `merge_request_url`. Set `rebuild_success=true`.

If the commit, push, or MR creation fails, set `rebuild_success=false` with the error and continue to Step 6.

### Step 6: Comment in JIRA

If `dry_run` is true, end the workflow.

Otherwise, post a comment to **each issue** in `all_jira_issues` using `add_jira_comment`:
- If the rebuild **succeeded**: post the `merge_request_url` (or `"Rebuild completed successfully"` if no MR was created).
- If the rebuild **failed**: post `"Agent failed to perform a rebuild: <error>"`.

If commenting on a consolidated issue fails, log a warning but continue with the remaining issues.

---

## Output Schema

The final output must be a JSON object:

```json
{
    "rebuild_success": true,
    "merge_request_url": "https://gitlab.com/...",
    "error": null
}
```

On failure:

```json
{
    "rebuild_success": false,
    "merge_request_url": null,
    "error": "Specific details about the error"
}
```
