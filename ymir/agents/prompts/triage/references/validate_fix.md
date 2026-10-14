---
name: validate_fix
description: >-
  Full procedure for validating backport patch/commit URLs: patch-format checks,
  CVE-ID matching, follow-up-commit rules, merged-only rules, and
  postponed_pr_pending handling. Use when you have candidate patch URL(s) for a
  backport and before deciding the outcome.
---

# Validate the Fix and URL

Apply this procedure to every patch/commit URL you propose for a backport.

* First, make sure the URL is an actual patch/commit link, not an issue or bug tracker reference
  (e.g. reject URLs containing /issues/, /bug/, bugzilla, jira, /tickets/)
* Use `get_github_patch` for HTTPS GitHub URLs and `get_patch_from_url` for all other
  patch/commit URLs you intend to use
* Prefer the authenticated GitHub MCP tools over shell commands for every supported
  GitHub operation: use `get_github_patch` for patches,
  `get_github_pull_request` for pull-request metadata, and `get_github_compare` for
  comparisons. This avoids exhausting the shared unauthenticated GitHub rate limit.
  Never use `curl`, `wget`, or another unauthenticated shell request when one of
  these tools supports the operation. If an unauthenticated GitHub request is
  rate-limited or times out, retry it through the corresponding authenticated tool.
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
     - If the CVE description quotes specific code expressions or
       variable names involved in the vulnerability, verify that the
       patch modifies those exact expressions — not just the same
       file or neighboring functions
  3. **For CVE issues - Verify CVE ID match**: If the issue is a CVE (contains CVE-YYYY-NNNNN):
     - Check if the patch content or commit message mentions the EXACT CVE ID
     - If the CVE ID is NOT mentioned in the patch, verify that:
       * The vulnerability description in the CVE matches what the patch fixes
       * The code changes address the specific vulnerability type
         (buffer overflow, integer overflow, etc.)
       * The affected functions/files align with the CVE details
     - **Multiple CVEs in one issue**: When the Jira issue covers multiple CVEs,
       verify you have the correct patches for ALL of them — not just one.
       Include ALL CVE IDs in the `cve_id` output field so the downstream
       backport agent knows the full scope and applies all related patches.
     - If you cannot confirm the patch matches the CVE, search for alternative patches or
       request clarification
* Only proceed with URLs that contain valid patch content AND address the specific issue
* If the content is not a proper patch or doesn't fix the issue, continue searching for other fixes
* **Only use merged/accepted fixes**: Patches must come from commits that have been
  merged into the upstream repository (or Fedora). Do NOT use patches from:
  - Unmerged pull requests or merge requests
  - Bug tracker attachments or discussion threads (e.g. SourceForge, Bugzilla attachments)
  - Mailing list proposals that have not been accepted upstream
  - Forks or personal branches that are not part of the official repository
  If you find a relevant but unmerged patch during your investigation, mention it in the
  clarification-needed note so a human can evaluate it, but do not use it as the basis
  for a backport decision.
* **Unmerged upstream fixes — use postponed_pr_pending**: If during your
  investigation you find a relevant fix in an UNMERGED GitLab merge request
  or GitHub pull request that addresses the issue but has not been merged yet:
  - Validate that the PR/MR is still open (not closed/abandoned)
  - Use resolution "postponed_pr_pending" instead of "clarification-needed"
  - Set blocker_references to a JSON array with the full PR/MR URL:
    - GitLab MR: `["https://gitlab.com/org/repo/-/merge_requests/123"]`
    - GitHub PR: `["https://github.com/org/repo/pull/123"]`
  - Set pending_issues to [<this issue key>]
  - This does NOT apply to other platforms (e.g. Bitbucket, SourceForge)
  The sweep system will monitor the PR/MR and re-triage once it merges.
* **Check for follow-up commits**: After identifying a valid fix, you MUST check
  whether there are follow-up commits that are **necessary to make the fix
  correct and complete**. A follow-up commit should be included ONLY when:
  - It fixes a bug, crash, or regression introduced by the primary fix
    (i.e. the primary fix does not work correctly without it)
  - It completes the fix for the same issue/CVE when the primary commit
    only addressed part of the problem (e.g. a second code path still
    vulnerable to the same CVE)
  - Its commit message explicitly states it fixes or corrects the primary
    commit (e.g. "fix regression from ...", "complete fix for CVE-...")
  A follow-up commit should **NOT** be included when:
  - It adds additional hardening, defensive checks, or robustness
    improvements beyond what is needed to fix the reported issue
  - It adds or improves tests, comments, documentation, or code style
    without changing the fix logic
  - It refactors or improves the area touched by the fix but is not
    required for the fix to work (nice-to-have improvements)
  - It addresses a separate issue or vulnerability, even if it touches
    the same files or functions
  The key question is: "Does the primary fix fully resolve the reported
  issue/CVE on its own?" If yes, subsequent commits that improve,
  harden, or extend the fix are out of scope and must be excluded.
  Use multiple search strategies — any single strategy can miss commits:
  1. Ancestry search on affected files:
     `git log <primary-fix>..HEAD -- <affected-files>`
  2. Author-based search — find commits by the same author on the
     same files within ~2 months after the primary fix:
     `git log --all --author="<author>" --since="<fix-date>" --until="<fix-date+2months>" -- <files>`
  3. Keyword search — search for the issue/CVE ID or related
     function names in commit messages:
     `git log --all --grep="<issue-number-or-CVE-ID>"`
  If you find follow-up commits, evaluate each one against the criteria
  above. For those that qualify, validate them the same way (use
  `get_github_patch` for HTTPS GitHub URLs and `get_patch_from_url` otherwise,
  then verify they are real patches) and include them
  in your patch_urls list, ordered chronologically (earliest first).
* **Prefer individual commit URLs; collapse to PR/MR when appropriate**:
  Start by searching for individual fixing commits and use their
  `.patch` URLs in your `patch_urls` list. This is the default.
  However, use a single PR/MR `.patch` URL instead when either:
  - You discover that ALL the fixing commits you collected originate
    from the same pull request or merge request — in that case,
    replace the individual commit URLs with the PR/MR `.patch` URL, OR
  - The maintainer rules for the package explicitly instruct you to
    look for pull requests or merge requests as fixes.
  When using a PR/MR URL, construct it as:
  - GitHub PR: `https://github.com/org/repo/pull/N.patch`
  - GitLab MR: `https://gitlab.com/org/repo/-/merge_requests/N.patch`
  Fetch and validate GitHub URLs via `get_github_patch`; use
  `get_patch_from_url` for all other URLs before using them.
* **Use commits from the target branch, not the PR source branch**:
  When a PR/MR is merged via rebase or squash on GitHub/GitLab, the
  commit hashes on the target branch (e.g. `main`, `master`) differ
  from the hashes shown in the PR's "Commits" tab, which belong to
  the source/fork branch. Always use the commit hash that landed on
  the default branch, not the one from the PR branch.
  To find the correct hash on GitHub: look for "merged commit <hash>
  into <target-branch>" in the PR timeline, or check `git log` on
  the default branch. The fork-branch commit may still be accessible
  via its URL, but it is not the canonical merged commit and should
  not be used for backporting.
