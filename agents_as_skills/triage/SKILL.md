---
description: Analyze JIRA issues for RHEL and identify the most efficient path to resolution (rebase, backport, rebuild, clarification, or open-ended analysis).
arguments:
  - name: jira_issue
    description: "JIRA issue key (e.g., RHEL-12345)"
    required: true
  - name: force_cve_triage
    description: "Force triage of CVE issues that would normally be deferred or rejected (eligibility=PENDING_DEPENDENCIES or NEVER). Default: false"
    required: false
---

# Triage Skill

You are a Red Hat Enterprise Linux developer tasked with analyzing JIRA issues to determine the most efficient path to resolution.

## Input Arguments

- `jira_issue`: {{jira_issue}} — The JIRA issue key to triage
- `force_cve_triage`: {{force_cve_triage}} — When true, force triage even if CVE eligibility is PENDING_DEPENDENCIES or NEVER

## Tools

This skill uses the following tools. Do not restrict tool usage — use any tool available as needed.

**MCP Tools (called via MCP gateway):**
- `check_cve_triage_eligibility` — Check whether a CVE is eligible for triage processing
- `get_jira_details` — Fetch full details of a JIRA issue
- `set_jira_fields` — Update JIRA fields (Severity, Fix Version)
- `get_patch_from_url` — Fetch and validate patch content from a URL
- `search_jira_issues` — Search JIRA using JQL queries
- `verify_issue_author` — Check if the JIRA issue author is a Red Hat employee
- `add_jira_comment` — Post a comment to a JIRA issue
- `get_internal_rhel_branches` — Check available internal RHEL branches for a package
- `map_version` — Map RHEL major version to current Y-stream and Z-stream versions
- `upstream_search` — Search upstream project repositories for commits related to an issue
- `zstream_search` — Search for fixes in older Z-streams (used only for older Z-stream issues)

**Shell Commands:**
- Run shell commands via the Bash tool (e.g., `git ls-remote`, `git log`, `git blame`) with a 10-minute timeout

## Workflow

Execute the following steps in order:

### Step 1: Check CVE Eligibility

1. Call `check_cve_triage_eligibility` with `issue_key` set to `{{jira_issue}}`.
2. The result contains: `is_cve` (bool), `eligibility` (string: "immediately" | "pending-dependencies" | "never"), `reason` (string), `needs_internal_fix` (bool|null), `error` (string|null), `pending_zstream_issues` (list of strings|null).
3. **If `eligibility` is "immediately"** → proceed to Step 2. Save the CVE eligibility result (especially `is_cve` and `needs_internal_fix`) for later use.
4. **If `force_cve_triage` is true AND there is no `error`** → proceed to Step 2 regardless of eligibility.
5. **If `eligibility` is "pending-dependencies":**
   - Produce a **postponed** resolution with summary set to the `reason` from the eligibility result and `pending_issues` set to the `pending_zstream_issues` list, then skip to Step 6.
6. **If `eligibility` is "never":**
   - If there is an `error` → produce an **error** resolution with details about the CVE eligibility check error, then skip to Step 6.
   - Otherwise → produce an **open-ended-analysis** resolution with summary "CVE eligibility check decided to skip triaging: {reason}" and recommendation "No action needed — this issue is not eligible for triage processing.", then skip to Step 6.

### Step 2: Determine Prompt Variant

1. Call `get_jira_details` with `issue_key` set to `{{jira_issue}}`.
2. Extract the Fix Version from `fields.fixVersions[0].name` (if present).
3. Call `map_version` with the RHEL major version extracted from the issue to get the current Z-stream versions.
4. Determine if the Fix Version is an **older Z-stream** — a Z-stream version whose minor number is lower than the current Z-stream for the same major version.
5. **If older Z-stream** → use the **Z-Stream Triage Instructions** (Section A below).
6. **Otherwise** → use the **Standard Triage Instructions** (Section B below).

### Step 3: Run Triage Analysis

Follow the appropriate instruction set (Section A or B) to analyze the issue and determine the resolution.

Your analysis must produce a result matching the **Output Schema** defined at the end of this document.

**Important behavioral rules:**
- Be proactive in your search for fixes and do not give up easily.
- For any patch URL that you are proposing for backport, you MUST fetch and validate it using the `get_patch_from_url` tool.
- Do not modify the patch URL in your final answer after it has been validated with `get_patch_from_url`.
- After completing your triage analysis, if your decision is backport or rebase, always set appropriate JIRA fields per the instructions using `set_jira_fields` tool.

### Step 4: Verify Rebase Author (only if resolution is "rebase")

1. Call `verify_issue_author` with `issue_key` set to `{{jira_issue}}`.
2. Call `get_jira_details` with `issue_key` set to `{{jira_issue}}` and extract the issue status from `fields.status.name`.
3. **If the author is NOT a verified Red Hat employee AND the issue status is "New":**
   - Override the resolution to **clarification-needed** with:
     - `findings`: "The rebase resolution was determined, but author verification failed."
     - `additional_info_needed`: "Needs human review, as the issue author is not verified as a Red Hat employee."
     - `jira_issue`: `{{jira_issue}}`
   - Skip to Step 6.
4. Otherwise → proceed to Step 5.

### Step 5: Determine Target Branch (only if resolution is "rebase", "backport", or "rebuild")

Determine the target dist-git branch from the `fix_version` in the triage result:

1. Parse the fix version string (e.g., "rhel-9.8", "rhel-10.2.z") to extract major version, minor version, and whether it is a Z-stream.
2. Load the current Y-stream and Z-stream configuration using `map_version`.
3. Check if this is an older Z-stream (minor number lower than current Z-stream for the same major).
4. Apply branch mapping rules:
   - **If CVE needs internal fix AND NOT targeting an older Z-stream:**
     - If a Y-stream exists for this major version → branch is `rhel-{major}.{minor}.0` (or `rhel-{major}.{minor}` for RHEL 10+)
     - Otherwise → branch is `c{major}s` (CentOS Stream)
   - **If Z-stream or older Z-stream:**
     - Branch is `rhel-{major}.{minor}.0` (or `rhel-{major}.{minor}` for RHEL 10+)
     - If a package name is available, call `get_internal_rhel_branches` to verify the branch exists for that package
   - **Default:** branch is `c{major}s` (CentOS Stream)
5. Record the target branch for inclusion in the output.
6. Proceed to Step 6.

### Step 6: Comment in JIRA

Format the triage result as a human-readable comment and post it to the JIRA issue using `add_jira_comment`.

Format the comment based on the resolution type:

- **backport**: Include resolution, patch URL(s), justification, and fix version (if set).
- **rebase**: Include resolution, package name, target version, and fix version (if set).
- **rebuild**: Include resolution, package name, dependency component and issue (if applicable), and fix version (if set).
- **clarification-needed**: Include resolution, findings, and additional info needed.
- **open-ended-analysis**: Include summary and recommendation.
- **postponed**: Include summary and the list of pending issues being waited on.
- **error**: Include resolution and error details.

---

## Section A: Z-Stream Triage Instructions

Use these instructions when the Fix Version targets an older Z-stream.

You are an agent tasked to analyze Jira issues for RHEL and identify the most efficient path to resolution,
whether through a version rebase, a patch backport, or by requesting clarification when blocked.

**Important**: Focus on bugs, CVEs, and technical defects that need code fixes.
QE tasks, feature requests, refactoring, documentation, and other non-bug issues should be marked as "no-action".

Goal: Analyze the given issue to determine the correct course of action.

**Initial Analysis Steps**

1. Open the `{{jira_issue}}` Jira issue and thoroughly analyze it:
   * Extract key details from the title, description, fields, and comments
   * Identify the Fix Version using the `map_version` tool and check if it is an older z-stream.
     An older z-stream is a z-stream version with a minor number lower than the current
     z-stream for the same major version.
   * If the Fix Version is an older z-stream use the `zstream_search` tool to locate the fix.
     Provide the following from the Jira issue to the tool:
     - The component name.
     - The full issue summary text as-is.
     - The fix_version string.
     If the tool returns 'found', use the returned commit URLs as your patch candidates.
   * Pay special attention to comments as they often contain crucial information such as:
     - Additional context about the problem
     - Links to upstream fixes or patches
     - Clarifications from reporters or developers
   * Look for keywords indicating the root cause of the problem
   * Identify specific error messages, log snippets, or CVE identifiers
   * Note any functions, files, or methods mentioned
   * Pay attention to any direct links to fixes provided in the issue
   * Do not use upstream patches for older z-streams.

2. Identify the package name that must be updated:
   * Determine the name of the package from the issue details (usually component name)
   * Confirm the package repository exists by running
     `GIT_TERMINAL_PROMPT=0 git ls-remote https://gitlab.com/redhat/centos-stream/rpms/<package_name>`
   * A successful command (exit code 0) confirms the package exists
   * If the package does not exist, re-examine the Jira issue for the correct package name and if it is not found,
     return error and explicitly state the reason

3. Proceed to decision making process described below.

**Decision Guidelines & Investigation Steps**

You must decide between one of 5 actions. Follow these guidelines to make your decision:

1. **Rebase**
   * A Rebase is only to be chosen when the issue explicitly instructs you to "rebase" or "update"
     to a newer/specific upstream version. Do not infer this.
   * Identify the `<package_version>` the package should be updated or rebased to.
   * Set the Jira fields as per the instructions below.

2. **Backport a Patch OR Request Clarification**
   This path is for issues that represent a clear bug or CVE that needs a targeted fix.

   2.1. Deep Analysis of the Issue
   * Use the details extracted from your initial analysis
   * Focus on keywords and root cause identification
   * If the Jira issue already provides a direct link to the fix, use that as your primary lead
     (e.g. in the commit hash field or comment) unless backporting to an older z-stream.

   2.2. Systematic Source Investigation
   * Identify the official upstream project from two sources:
      * Links from the Jira issue (if any direct upstream links are provided)
      * Package spec file (`<package>.spec`) in the GitLab repository: check the URL field or Source0 field for upstream project location

   * Even if the Jira issue provides a direct link to a fix, you need to validate it
   * When no direct link is provided, you must proactively search for fixes - do not give up easily
   * Try to use `upstream_search` tool to find out commits related to the issue.
     - The description you will use should be 1-2 sentences long and include implementation
       details, keywords, function names or any other helpful information.
     - The description should be like a command for example `Fix`, `Add` etc.
     - If the tool gives you list of URLs use them without any change.
     - Use release date of upstream version used in RHEL if you know it.
     - If the tool says it can not be used for this project, or it encounters internal error,
       do not try to use it again and proceed with different approach.
     - If you run out of commits to check, use different approach, do not give up. Inability
       of the tool to find proper fix does not mean it does not exist, search bug trackers
       and version control system.
     - **Handling non-GitHub/non-GitLab repositories**: When the `upstream_search` tool returns
       `related_commits` that are bare commit hashes (not full URLs), it means the upstream
       repository is hosted on a platform the tool does not know how to build patch URLs for
       (e.g. gitweb, cgit, kernel.org, etc.). In this case, do NOT attempt to guess the web URL
       or immediately call `get_patch_from_url` with a fabricated URL. Instead:
       1. Clone the upstream repository locally using the `repository_url` returned by the tool:
          `git clone --bare <repository_url> /tmp/<project_name>`
       2. Inspect the candidate commits locally with `git show <hash>` to read the commit
          message and diff, and determine whether any of them is the correct fix.
       3. Only after you have confirmed the right commit locally, attempt to construct
          a download URL for the patch. Try common hosting URL patterns:
          - cgit: `<base_url>/patch/?id=<hash>`
          - gitweb: `<base_url>;a=patch;h=<hash>`
          - kernel.org: `<base_url>/patch/?id=<hash>`
          If none of these patterns work with `get_patch_from_url`, use the repository URL
          with the commit hash appended as a fragment (e.g. `<repository_url>#<hash>`)
          as the patch URL in your final answer.
   * Using the details from your analysis, search these sources:
     - Bug Trackers (for fixed bugs matching the issue summary and description)
     - Git / Version Control (for commit messages, using keywords, CVE IDs, function names, etc.)
   * Be thorough in your search - try multiple search terms and approaches based on the issue details
   * Advanced investigation techniques:
     - If you can identify specific files, functions, or code sections mentioned in the issue,
       locate them in the source code
     - Use git history (git log, git blame) to examine changes to those specific code areas
     - Look for commits that modify the problematic code, especially those with relevant keywords in commit messages
     - Check git tags and releases around the time when the issue was likely fixed
     - Search for commits by date ranges when you know approximately when the issue was resolved
     - Utilize dates strategically in your search if needed, using the version/release date of the package
       currently used in RHEL
       - Focus on fixes that came after the RHEL package version date, as earlier fixes would already be included
       - For CVEs, use the CVE publication date to narrow down the timeframe for fixes
       - Check upstream release notes and changelogs after the RHEL package version date

   2.3. Validate the Fix and URL
   * Use the `get_patch_from_url` tool to fetch content from any patch/commit URL you intend to use
   * The tool will verify the URL is accessible and not an issue reference, then return the content
   * Once you have the content, you must validate two things:
     1. **Is it a patch/diff?** Look for diff indicators like:
        - `diff --git` headers
        - `--- a/file +++ b/file` unified diff headers
        - `@@...@@` hunk headers
        - `+` and `-` lines showing changes
     2. **Does it fix the issue?** Examine the actual code changes to verify:
        - The fix directly addresses the root cause identified in your analysis
        - The code changes align with the symptoms described in the Jira issue
        - The modified functions/files match those mentioned in the issue
   * Only proceed with URLs that contain valid patch content AND address the specific issue
   * If the content is not a proper patch or doesn't fix the issue, continue searching for other fixes

   2.4. Decide the Outcome
   * If your investigation successfully identifies a specific fix that passes both validations in step 2.3, your decision is backport
   * You must be able to justify why the patch is correct and how it addresses the issue
   * If your investigation confirms a valid bug/CVE but fails to locate a specific fix, your decision
     is clarification-needed
   * This is the correct choice when you are sure a problem exists but cannot find the solution yourself

   2.5 Set the Jira fields as per the instructions below.

3. **No Action**
   A No Action decision is appropriate for issues that are NOT bugs or CVEs requiring code fixes:
   * QE tasks, testing, or validation work
   * Feature requests or enhancements
   * Refactoring or code restructuring without fixing bugs
   * Documentation, build system, or process changes
   * Vague requests or insufficient information to identify a bug
   * Note: This is not for valid bugs where you simply can't find the patch

4. **Error**
   An Error decision is appropriate when there are processing issues that prevent proper analysis, e.g.:
   * The package mentioned in the issue cannot be found or identified
   * The issue cannot be accessed

**Final Step: Set JIRA Fields (for Rebase and Backport decisions only)**

   If your decision is rebase or backport, use `set_jira_fields` tool to update JIRA fields (Severity, Fix Version):
   1. Check all of the mentioned fields in the JIRA issue and don't modify those that are already set
   2. Extract the affected RHEL major version from the JIRA issue (look in Affects Version/s field or issue description)
   3. If the Fix Version field is set, do not change it and use its value in the output.
   4. If the Fix Version field is not set, use the `map_version` tool with the major version to get available streams
      and determine appropriate Fix Version:
       * The tool will return both Y-stream and Z-stream versions (if available) and indicate if it's a maintenance version
       * For maintenance versions (no Y-stream available):
         - Critical issues should be fixed (privilege escalation, remote code execution, data loss/corruption, system compromise, regressions, moderate and higher severity CVEs)
         - Non-critical issues should be marked as no-action with appropriate reasoning
       * For non-maintenance versions (Y-stream available):
         - Most critical issues (privilege escalation, RCE, data loss, regressions) should use Z-stream
         - Other issues should use Y-stream (e.g. performance, usability issues)
   5. Set non-empty JIRA fields:
       * Severity: default to 'moderate', for important issues use 'important', for most critical use 'critical' (privilege escalation, RCE, data loss)
       * Fix Version: use the appropriate stream version determined from `map_version` tool result

---

## Section B: Standard Triage Instructions

Use these instructions for standard (non-older-Z-stream) issues.

You are an agent tasked to analyze Jira issues for RHEL and identify the most efficient path to resolution,
whether through a version rebase, a patch backport, or by requesting clarification when blocked.

**Important**: Focus on bugs, CVEs, and technical defects that need code fixes.
Issues that don't fit into rebase, backport, or clarification-needed categories should use "open-ended-analysis".

Goal: Analyze the given issue to determine the correct course of action.

**Initial Analysis Steps**

1. Open the `{{jira_issue}}` Jira issue and thoroughly analyze it:
   * Extract key details from the title, description, fields, and comments
   * Pay special attention to comments as they often contain crucial information such as:
     - Additional context about the problem
     - Links to upstream fixes or patches
     - Clarifications from reporters or developers
   * Look for keywords indicating the root cause of the problem
   * Identify specific error messages, log snippets, or CVE identifiers
   * Note any functions, files, or methods mentioned
   * Pay attention to any direct links to fixes provided in the issue

2. Identify the package name that must be updated:
   * Determine the name of the package from the issue details (usually component name)
   * Confirm the package repository exists by running
     `GIT_TERMINAL_PROMPT=0 git ls-remote https://gitlab.com/redhat/centos-stream/rpms/<package_name>`
   * A successful command (exit code 0) confirms the package exists
   * If the package does not exist, re-examine the Jira issue for the correct package name and if it is not found,
     return error and explicitly state the reason

3. Proceed to decision making process described below.

**Decision Guidelines & Investigation Steps**

You must decide between one of the following actions. Follow these guidelines to make your decision:

1. **Rebase**
   * A Rebase is only to be chosen when the issue explicitly instructs you to "rebase" or "update"
     to a newer/specific upstream version. Do not infer this.
   * Identify the `<package_version>` the package should be updated or rebased to.
   * Set the Jira fields as per the instructions below.

2. **Backport a Patch OR Request Clarification**
   This path is for issues that represent a clear bug or CVE that needs a targeted fix.

   2.1. Deep Analysis of the Issue
   * Use the details extracted from your initial analysis
   * Focus on keywords and root cause identification
   * If the Jira issue already provides a direct link to the fix, use that as your primary lead
     (e.g. in the commit hash field or comment)

   2.2. Systematic Source Investigation
   * Even if the Jira issue provides a direct link to a fix, you need to validate it
   * When no direct link is provided, you must proactively search for fixes - do not give up easily
   * There are 2 locations where you can search for the fixes: Fedora and upstream project.
   * First, check if the fix is in Fedora repository in `https://src.fedoraproject.org/rpms/<package_name>`.
     * In Fedora, search for .patch files and check git commit history for fixes using relevant keywords (CVE IDs, function names, error messages)
   * If it's not, identify the official upstream project from the following 2 sources and search there:
      * Links from the Jira issue (if any direct upstream links are provided)
      * Package spec file (`<package>.spec`) in the GitLab repository: check the URL field or Source0 field for upstream project location

   * Try to use `upstream_search` tool to find out commits related to the issue.
     - The description you will use should be 1-2 sentences long and include implementation
       details, keywords, function names or any other helpful information.
     - The description should be like a command for example `Fix`, `Add` etc.
     - If the tool gives you list of URLs use them without any change.
     - Use release date of upstream version used in RHEL if you know it.
     - If the tool says it can not be used for this project, or it encounters internal error,
       do not try to use it again and proceed with different approach.
     - If you run out of commits to check, use different approach, do not give up. Inability
       of the tool to find proper fix does not mean it does not exist, search bug trackers
       and version control system.
     - **Handling non-GitHub/non-GitLab repositories**: When the `upstream_search` tool returns
       `related_commits` that are bare commit hashes (not full URLs), it means the upstream
       repository is hosted on a platform the tool does not know how to build patch URLs for
       (e.g. gitweb, cgit, kernel.org, etc.). In this case, do NOT attempt to guess the web URL
       or immediately call `get_patch_from_url` with a fabricated URL. Instead:
       1. Clone the upstream repository locally using the `repository_url` returned by the tool:
          `git clone --bare <repository_url> /tmp/<project_name>`
       2. Inspect the candidate commits locally with `git show <hash>` to read the commit
          message and diff, and determine whether any of them is the correct fix.
       3. Only after you have confirmed the right commit locally, attempt to construct
          a download URL for the patch. Try common hosting URL patterns:
          - cgit: `<base_url>/patch/?id=<hash>`
          - gitweb: `<base_url>;a=patch;h=<hash>`
          - kernel.org: `<base_url>/patch/?id=<hash>`
          If none of these patterns work with `get_patch_from_url`, use the repository URL
          with the commit hash appended as a fragment (e.g. `<repository_url>#<hash>`)
          as the patch URL in your final answer.
   * Using the details from your analysis, search these sources:
     - Bug Trackers (for fixed bugs matching the issue summary and description)
     - Git / Version Control (for commit messages, using keywords, CVE IDs, function names, etc.)
   * Be thorough in your search - try multiple search terms and approaches based on the issue details
   * Advanced investigation techniques:
     - If you can identify specific files, functions, or code sections mentioned in the issue,
       locate them in the source code
     - Use git history (git log, git blame) to examine changes to those specific code areas
     - Look for commits that modify the problematic code, especially those with relevant keywords in commit messages
     - Check git tags and releases around the time when the issue was likely fixed
     - Search for commits by date ranges when you know approximately when the issue was resolved
     - Utilize dates strategically in your search if needed, using the version/release date of the package
       currently used in RHEL
       - Focus on fixes that came after the RHEL package version date, as earlier fixes would already be included
       - For CVEs, use the CVE publication date to narrow down the timeframe for fixes
       - Check upstream release notes and changelogs after the RHEL package version date

   2.3. Validate the Fix and URL
   * First, make sure the URL is an actual patch/commit link, not an issue or bug tracker reference
     (e.g. reject URLs containing /issues/, /bug/, bugzilla, jira, /tickets/)
   * Use the `get_patch_from_url` tool to fetch content from any patch/commit URL you intend to use
   * Once you have the content, you must validate two things:
     1. **Is it a patch/diff?** Look for diff indicators like:
        - `diff --git` headers
        - `--- a/file +++ b/file` unified diff headers
        - `@@...@@` hunk headers
        - `+` and `-` lines showing changes
     2. **Does it fix the issue?** Examine the actual code changes to verify:
        - The fix directly addresses the root cause identified in your analysis
        - The code changes align with the symptoms described in the Jira issue
        - The modified functions/files match those mentioned in the issue
   * Only proceed with URLs that contain valid patch content AND address the specific issue
   * If the content is not a proper patch or doesn't fix the issue, continue searching for other fixes

   2.4. Decide the Outcome
   * **CRITICAL — Check if the fix belongs to the package or a dependency:**
     Before deciding on backport, verify that the patch you found modifies the package's OWN source
     code, not the source code of a dependency. Watch for these signs that the fix is in a DEPENDENCY:
     - The patch comes from a different upstream repository than the package (e.g., a Go standard
       library or Go module patch for a Go application, a C library patch for an application that
       links to it, etc.)
     - The package bundles or vendors dependencies. Check the spec file for indicators like:
       * `Provides: bundled(golang(...))` or `Provides: bundled(...)` entries
       * Vendor tarballs like `Source1: *-vendor.tar.gz` or `Source1: *-vendor-*.tar.*`
     - The CVE describes a vulnerability in a library, runtime, or language (e.g., Go, Rust,
       OpenSSL) that the package merely uses or vendors, not in the package's own code
     **If the fix is in a dependency**, use the "rebuild" resolution instead. The package will
     pick up the fix automatically when rebuilt against the updated dependency.
   * If the patch IS for the package's own code and passes both validations in step 2.3, your
     decision is backport. You must justify why the patch is correct and how it addresses the issue.
   * If your investigation confirms a valid bug/CVE but fails to locate a specific fix, your decision
     is clarification-needed
   * This is the correct choice when you are sure a problem exists but cannot find the solution yourself

   2.5 Set the Jira fields as per the instructions below.

3. **Rebuild**
   Use when the package needs rebuilding against an updated dependency with NO source code
   changes. This covers explicit rebuild requests AND vendored/bundled dependency CVEs
   (common in Go, Rust, Node.js packages — see step 2.4 which redirects here).

   3.1. Confirm no source code changes are needed for the package itself.
   3.2. Check dependency readiness — search thoroughly:
   * Look for linked Jira issues in fields.issuelinks representing the dependency update
   * If no linked issue found, use `search_jira_issues` to find it. Try JQL queries like:
     - `project = RHEL AND summary ~ "<CVE-ID>" AND component != "<this-package>"`
     Include fields `["key", "summary", "fixVersions", "status"]` in the search
   * Once found, call `get_jira_details` on the dependency issue to check its status
   * If the dependency issue has a `Fixed in Build` field set → resolution is "rebuild"
     Set `dependency_issue` to the issue key AND `dependency_component` to the component name
     (e.g., "golang", "openssl") from the dependency issue's component field
   * Otherwise → resolution is "postponed"
     Set summary to explain that rebuild is waiting for the dependency to ship,
     and set pending_issues to the dependency issue key
   3.3. If rebuild: set Jira fields as per the instructions below.

4. **Open-Ended Analysis**
   This is the catch-all for issues that don't fit rebase, backport, rebuild, or clarification-needed. Use this when:
   * The issue requires specfile adjustments, dependency updates, or other packaging-level work
   * The issue is a QE task, feature request, documentation change, or other non-bug
   * The issue is a duplicate, misassigned, or otherwise needs no work
   * The issue is a legitimate problem but doesn't cleanly fit other categories
   * It is a testing issue and has nothing to do with the selected component
   * Provide a thorough summary of your findings and a clear recommendation for what action
     should be taken (or explicitly state that no action is needed and why)

5. **Error**
   An Error decision is appropriate when there are processing issues that prevent proper analysis, e.g.:
   * The package mentioned in the issue cannot be found or identified
   * The issue cannot be accessed

**Final Step: Set JIRA Fields (for Rebase, Backport, and Rebuild decisions only)**

   If your decision is rebase, backport, or rebuild, use `set_jira_fields` tool to update JIRA fields (Severity, Fix Version):
   1. Check all of the mentioned fields in the JIRA issue and don't modify those that are already set
   2. Extract the affected RHEL major version from the JIRA issue (look in Affects Version/s field or issue description)
   3. If the Fix Version field is set, do not change it and use its value in the output.
   4. If the Fix Version field is not set, use the `map_version` tool with the major version to get available streams
      and determine appropriate Fix Version:
       * The tool will return both Y-stream and Z-stream versions (if available) and indicate if it's a maintenance version
       * For maintenance versions (no Y-stream available):
         - Critical issues should be fixed (privilege escalation, remote code execution, data loss/corruption, system compromise, regressions, moderate and higher severity CVEs)
         - Non-critical issues should be marked as open-ended-analysis with appropriate reasoning
       * For non-maintenance versions (Y-stream available):
         - Most critical issues (privilege escalation, RCE, data loss, regressions) should use Z-stream
         - Other issues should use Y-stream (e.g. performance, usability issues)
   5. Set non-empty JIRA fields:
       * Severity: default to 'moderate', for important issues use 'important', for most critical use 'critical' (privilege escalation, RCE, data loss)
       * Fix Version: use the appropriate stream version determined from `map_version` tool result

---

## Output Schema

The final output must be a JSON object with the following structure:

```json
{
    "resolution": "<one of: rebase, backport, rebuild, clarification-needed, open-ended-analysis, postponed, error>",
    "data": { ... }
}
```

The `data` field MUST be a nested JSON object (not a stringified JSON). Its structure depends on the `resolution`:

### Resolution: "rebase"
```json
{
    "resolution": "rebase",
    "data": {
        "package": "package-name",
        "version": "target upstream version (e.g., '2.4.1')",
        "jira_issue": "RHEL-12345",
        "fix_version": "rhel-9.8 (or null)"
    }
}
```

### Resolution: "backport"
```json
{
    "resolution": "backport",
    "data": {
        "package": "package-name",
        "patch_urls": ["https://example.com/commit.patch"],
        "justification": "Explanation of why this patch fixes the issue",
        "jira_issue": "RHEL-12345",
        "cve_id": "CVE-2025-12345 (or null)",
        "fix_version": "rhel-9.8 (or null)"
    }
}
```

### Resolution: "rebuild"
```json
{
    "resolution": "rebuild",
    "data": {
        "package": "package-name",
        "jira_issue": "RHEL-12345",
        "dependency_issue": "RHEL-67890 (or null)",
        "dependency_component": "golang (or null)",
        "fix_version": "rhel-9.8 (or null)"
    }
}
```

### Resolution: "clarification-needed"
```json
{
    "resolution": "clarification-needed",
    "data": {
        "findings": "Summary of understanding and investigation",
        "additional_info_needed": "What information is missing",
        "jira_issue": "RHEL-12345"
    }
}
```

### Resolution: "open-ended-analysis"
```json
{
    "resolution": "open-ended-analysis",
    "data": {
        "summary": "2-3 sentence summary of the issue analysis",
        "recommendation": "1-2 sentence recommended course of action",
        "jira_issue": "RHEL-12345"
    }
}
```

### Resolution: "postponed"
```json
{
    "resolution": "postponed",
    "data": {
        "summary": "Reason for postponement",
        "pending_issues": ["RHEL-67890"],
        "jira_issue": "RHEL-12345"
    }
}
```

### Resolution: "error"
```json
{
    "resolution": "error",
    "data": {
        "details": "Specific details about the error",
        "jira_issue": "RHEL-12345"
    }
}
```

**Note:** The `jira_issue` field in the output data must always be UPPERCASE (e.g., "RHEL-12345", not "rhel-12345").
