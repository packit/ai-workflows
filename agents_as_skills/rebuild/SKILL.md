---
name: rebuild
description: Rebuild packages against updated dependencies in the RHEL ecosystem — release bump, changelog, and merge request creation with no source code changes. Supports consolidating multiple sibling Jira issues into a single rebuild MR.
---

# Rebuild Skill

You are a Red Hat Enterprise Linux developer performing an end-to-end rebuild of a package against updated dependencies. This workflow makes no source code changes — it only bumps the release, adds a changelog entry, and opens a merge request. When consolidated sibling issues are provided, a single rebuild MR resolves all of them.

## Input Arguments

- `package`: {{package}}
- `dist_git_branch`: {{dist_git_branch}}
- `jira_issue`: {{jira_issue}}
- `fix_version`: {{fix_version}}
- `justification`: {{justification}}
- `triage_summary`: {{triage_summary}}
- `dependency_issue`: {{dependency_issue}}
- `dependency_component`: {{dependency_component}}
- `consolidated_issues`: {{consolidated_issues}}
- `consolidation_summary`: {{consolidation_summary}}
- `side_tag`: {{side_tag}}
- `dry_run`: {{dry_run}}

## Derived Values

Compute these at the start:

- `all_jira_issues`: a list starting with `{{jira_issue}}`, followed by the `issue_key` of each item in `consolidated_issues`. Example: `["RHEL-12345", "RHEL-67890", "RHEL-11111"]`.
- `all_jira_issues_str`: the above joined by commas. Example: `"RHEL-12345, RHEL-67890, RHEL-11111"`.
- `all_dependency_components`: the unique set of dependency component names, combining `dependency_component` (if set) with `dependency_component` from each consolidated issue (if set). Sort alphabetically.
- `all_dependency_issues`: the unique set of dependency issue keys, combining `dependency_issue` (if set) with `dependency_issue` from each consolidated issue (if set). Sort alphabetically.

## Tools

This skill uses the following tools. Do not restrict tool usage — use any tool available as needed.

**MCP Tools (called via MCP gateway):**
- `fork_repository` — Fork a dist-git repository on GitLab
- `clone_repository` — Clone a Git repository to a local path
- `create_zstream_branch` — Create a z-stream branch for a package (non-CentOS Stream, non-modular branches only)
- `push_to_remote_repository` — Push a branch to a remote repository
- `open_merge_request` — Open a merge request against dist-git
- `add_merge_request_labels` — Add labels to an existing merge request
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

1. Determine the namespace from the branch and optional `dist_git_namespace` override:
   - If `dist_git_namespace` is explicitly set, use it as-is (`rhel` or `centos-stream`).
   - Otherwise, if `dist_git_branch` starts with `c` and ends with `s` (e.g., `c10s`, `c9s`): namespace is `centos-stream`.
   - Otherwise: namespace is `rhel`.
2. Fork the repository by calling `fork_repository` with `repository` = `https://gitlab.com/redhat/<namespace>/rpms/{{package}}`. Save the returned `fork_url`.
3. If the namespace is `rhel` (not CentOS Stream) **and** the branch is not a modular branch (i.e., does not start with `stream-`), call `create_zstream_branch` with `package` = `{{package}}` and `branch` = `{{dist_git_branch}}` to ensure the branch exists.
4. Clone the repository by calling `clone_repository` with the repository URL, `branch` = `{{dist_git_branch}}`, and a local clone path. Save `local_clone`.
5. Create a working branch: `git checkout -B automated-package-update-{{jira_issue}}` in `local_clone`. Save `update_branch` = `automated-package-update-{{jira_issue}}`.
6. Set the working directory to `local_clone`.

### Step 2: Update Release

Bump the Release field in the spec file `{{package}}.spec` for package `{{package}}` on branch `{{dist_git_branch}}`. This is a packaging-level increment (not a rebase).

If this fails, set `rebuild_success=false` with the error and skip to **Step 6: Comment in JIRA**.

### Step 3: Generate Changelog and Commit Message

1. Run `git diff --cached --stat` to see which files have been changed.
2. Examine changes in each file individually: `git diff --cached -- <filename>` (do NOT run `git diff --cached` without a path).
3. Determine the changes summary based on the dependency context:
   - If `all_dependency_components` is non-empty: the summary is `"Rebuild of {{package}} for <all_jira_issues_str> against updated <all_dependency_components joined by comma>. The changelog entry and commit title MUST mention <all_dependency_components joined by comma>."`
   - Otherwise: the summary is `"Rebuild of {{package}} against updated dependencies for <all_jira_issues_str>."`
4. Add a new changelog entry to the spec file using `add_changelog_entry`. Examine the previous changelog entries and try to use the same style. The entry should contain:
   - A short summary of the user-facing changes (not technical packaging details)
   - For the Jira reference line: find the last changelog entry NOT authored by Ymir (skip entries by "RHEL Packaging Agent" or "redhat-ymir-agent"). If it contains a `Resolves:` or `Related:` line, include one in your entry using the same tag and formatting. If it does not contain such a line, do NOT add one.
5. Generate a title for the commit message and merge request. It should be descriptive but no longer than 80 characters. Do NOT include any Jira issue references (e.g. RHEL-XXXXX) in the title.
6. Generate a description as a short paragraph for the commit message and merge request. Line length should not exceed 80 characters. Do NOT include `Resolves:` lines — JIRA references are appended separately. Do NOT include any Jira issue references in the description.

Save the `title` and `description` for Step 5.

### Step 4: Stage Changes

1. Stage the spec file using `git add --all {{package}}.spec`.

If this fails, set `rebuild_success=false` with the error and skip to **Step 6: Comment in JIRA**.

### Step 5: Commit, Push, and Open Merge Request

1. Check if anything is actually staged by running `git diff --cached --quiet`.
   - Exit code 0 means no staged changes (commit would be empty) — set `allow_empty=true`.
   - Exit code 1 means there are staged changes — set `allow_empty=false`.

2. Construct dependency component metadata line:
   - If `all_dependency_components` has one component: `Dependency: <component>`.
   - If `all_dependency_components` has multiple: `Dependencies: <component1>, <component2>, ...`.
   - Only include this line if `all_dependency_components` is non-empty.

3. Construct dependency issue metadata line:
   - If `all_dependency_issues` has one issue: `Dependency issue: <issue>`.
   - If `all_dependency_issues` has multiple: `Dependency issues: <issue1>, <issue2>, ...`.
   - Only include this line if `all_dependency_issues` is non-empty.

4. Construct the resolves line: `Resolves: <all_jira_issues_str>` (all issues comma-separated on one line).

5. Create a git commit with the following message:
   ```
   <title>

   <description>

   [Dependency: <dependency_components>]  ← only if all_dependency_components is non-empty
   Resolves: <all_jira_issues_str>

   This commit was created by Ymir, a Red Hat Enterprise Linux software maintenance AI agent.

   Assisted-by: Ymir
   ```

6. If `dry_run` is true, stop after the commit (do not push or create MR). Set `rebuild_success=true`.

7. Push the branch to the fork using `push_to_remote_repository` with:
   - `repository`: `fork_url`
   - `clone_path`: `local_clone`
   - `branch`: `update_branch`
   - `force`: true

8. Construct the MR description:
   ```
   <description>

   [Dependency: <dependency_components>]  ← only if all_dependency_components is non-empty
   [Dependency issue: <dependency_issues>]  ← only if all_dependency_issues is non-empty
   Jira: [<issue>](https://redhat.atlassian.net/browse/<issue>)  ← single issue
   ### Resolved Jira Issues               ← multiple issues (bullet browse links)
   [
   side-tag: <side_tag>
   ]  ← only if side_tag is set
   [
   <triage details>
   ]  ← only if justification or triage_summary is set (see below)
   [
   Sibling consolidation analysis:
   <consolidation_summary>
   ]  ← only if consolidation_summary is set


   ---

   > **Warning: AI-Generated MR**: Created by Ymir AI assistant. AI may make mistakes...
   ```

   **Triage details block**: If `justification` or `triage_summary` is set, include a collapsible details block:
   ```html
   <details>
   <summary>Triage Details</summary>

   [**Reasoning:**
   <triage_summary>]  ← only if triage_summary is set

   [**Justification:**
   <justification>]  ← only if justification is set

   </details>
   ```

   Do NOT put `Resolves:` in the MR description (use browse links instead) — `Resolves:` belongs in the commit message only.

9. Determine MR labels:
   - Always include `ymir_rebuild`.
   - Additionally include `target::zstream` if `fix_version` refers to a z-stream on an active CentOS Stream branch (i.e., `dist_git_branch` is a CentOS Stream branch and `fix_version` has a z-stream component).

10. Open a merge request using `open_merge_request` with:
    - `fork_url`: from Step 1
    - `target`: `{{dist_git_branch}}`
    - `source`: `update_branch` from Step 1
    - `title`: the title from Step 3
    - `description`: the MR description constructed above
    - `labels`: the labels from step 9

11. Save the `merge_request_url`. Set `rebuild_success=true`.

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
