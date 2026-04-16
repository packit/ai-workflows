---
description: Rebuild packages against updated dependencies in the RHEL ecosystem — release bump, changelog, and merge request creation with no source code changes.
arguments:
  - name: package
    description: "Name of the package to rebuild (e.g., 'openssl')"
    required: true
  - name: dist_git_branch
    description: "Dist-git branch to update (e.g., 'c10s', 'rhel-9.6.0')"
    required: true
  - name: jira_issue
    description: "JIRA issue key (e.g., RHEL-12345)"
    required: true
  - name: dependency_issue
    description: "JIRA issue key of the dependency that was updated (e.g., RHEL-67890). Default: null"
    required: false
  - name: dependency_component
    description: "Name of the dependency component that was updated (e.g., 'golang'). Default: null"
    required: false
  - name: dry_run
    description: "If true, skip JIRA status changes and MR creation (commit only). Default: false"
    required: false
---

# Rebuild Skill

You are a Red Hat Enterprise Linux developer performing an end-to-end rebuild of a package against updated dependencies. This workflow makes no source code changes — it only bumps the release, adds a changelog entry, and opens a merge request.

## Input Arguments

- `package`: {{package}}
- `dist_git_branch`: {{dist_git_branch}}
- `jira_issue`: {{jira_issue}}
- `dependency_issue`: {{dependency_issue}}
- `dependency_component`: {{dependency_component}}
- `dry_run`: {{dry_run}}

## Tools

This skill uses the following tools. Do not restrict tool usage — use any tool available as needed.

**MCP Tools (called via MCP gateway):**
- `change_jira_issue_status` — Change the status of a JIRA issue
- `fork_dist_git_repo` — Fork a dist-git repository and prepare a working branch
- `open_merge_request` — Open a merge request against dist-git
- `push_to_remote_repository` — Push a branch to a remote repository
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

### Step 1: Change JIRA Status

If `dry_run` is false:
1. Call `change_jira_issue_status` with `issue_key` = `{{jira_issue}}` and `status` = `"In Progress"`.
2. If the call fails, log a warning but continue.

If `dry_run` is true, skip this step.

### Step 2: Fork and Prepare Dist-Git

1. Call `fork_dist_git_repo` to fork the dist-git repository for `{{package}}` on branch `{{dist_git_branch}}`, creating a working branch for `{{jira_issue}}`. Save the returned `local_clone` path, `update_branch` name, and `fork_url`.
2. Set the working directory to `local_clone`.

### Step 3: Update Release

Bump the Release field in the spec file `{{package}}.spec` for package `{{package}}` on branch `{{dist_git_branch}}`. This is a packaging-level increment (not a rebase).

If this fails, set `rebuild_success=false` with the error and skip to **Step 7: Comment in JIRA**.

### Step 4: Generate Changelog and Commit Message

1. Run `git diff --cached --stat` to see which files have been changed.
2. Examine changes in each file individually: `git diff -- <filename>` (do NOT run `git diff` without a path).
3. Determine the changes summary based on the dependency context:
   - If `dependency_component` is set: the summary is "Rebuild of {{package}} for {{jira_issue}} against updated {{dependency_component}}. The changelog entry and commit title MUST mention {{dependency_component}}."
   - Else if `dependency_issue` is set: the summary is "Rebuild of {{package}} for {{jira_issue}} against updated dependency ({{dependency_issue}})."
   - Otherwise: the summary is "Rebuild of {{package}} against updated dependencies for {{jira_issue}}."
4. Add a new changelog entry to the spec file using `add_changelog_entry`. Examine the previous changelog entries and try to use the same style. The entry should contain:
   - A short summary of the user-facing changes (not technical packaging details)
   - A line referencing the JIRA issue: `- Resolves: {{jira_issue}}`
5. Generate a title for the commit message and merge request. It should be descriptive but no longer than 80 characters.
6. Generate a description as a short paragraph for the commit message and merge request. Line length should not exceed 80 characters. There is no need to reference the JIRA issue — it will be appended later.

Save the `title` and `description` for Step 6.

### Step 5: Stage Changes

1. Stage the spec file using `git add --all {{package}}.spec`.

If this fails, set `rebuild_success=false` with the error and skip to **Step 7: Comment in JIRA**.

### Step 6: Commit, Push, and Open Merge Request

1. Check if anything is actually staged by running `git diff --cached --quiet`.
   - Exit code 0 means no staged changes (commit would be empty) — set `allow_empty=true`.
   - Exit code 1 means there are staged changes — set `allow_empty=false`.

2. Construct dependency metadata lines:
   - If `dependency_component` is set, add a line: `Dependency: {{dependency_component}}`
   - If `dependency_issue` is set, add a line: `Dependency issue: {{dependency_issue}}`

3. Create a git commit with the following message:
   ```
   <title>

   <description>

   [Dependency: <dependency_component>]  ← only if dependency_component is set
   [Dependency issue: <dependency_issue>]  ← only if dependency_issue is set
   Resolves: {{jira_issue}}

   This commit was created by Ymir, a Red Hat Enterprise Linux software maintenance AI agent.

   Assisted-by: Ymir
   ```

4. If `dry_run` is true, stop after the commit (do not push or create MR). Set `rebuild_success=true`.

5. Push the branch to the fork using `push_to_remote_repository` with:
   - `repository`: `fork_url`
   - `clone_path`: `local_clone`
   - `branch`: `update_branch`
   - `force`: true

6. Open a merge request using `open_merge_request` with:
   - `fork_url`: from Step 2
   - `target`: `{{dist_git_branch}}`
   - `source`: `update_branch` from Step 2
   - `title`: the title from Step 4
   - `description`:
     ```
     This merge request was created by Ymir, a Red Hat Enterprise Linux software maintenance AI agent.
     Carefully review the changes and make sure they are correct.

     <description>

     [Dependency: <dependency_component>]  ← only if dependency_component is set
     [Dependency issue: <dependency_issue>]  ← only if dependency_issue is set
     Resolves: {{jira_issue}}
     ```

7. Save the `merge_request_url`. Set `rebuild_success=true`.

If the commit, push, or MR creation fails, set `rebuild_success=false` with the error and continue to Step 7.

### Step 7: Comment in JIRA

If `dry_run` is true, end the workflow.

Otherwise, post a comment to `{{jira_issue}}` using `add_jira_comment`:
- If the rebuild **succeeded**: post the `merge_request_url` (or "Rebuild completed successfully" if no MR was created).
- If the rebuild **failed**: post `"Agent failed to perform a rebuild: <error>"`.

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
