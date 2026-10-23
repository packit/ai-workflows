---
name: triage-rebuild
description: >-
  Full Rebuild procedure: search for the dependency ticket, validate its
  fixVersion, verify Fixed in Build / dropped resolutions, and handle the
  postponed_dependency outcome. Use when the resolution is rebuild, i.e. the
  package must be rebuilt against an updated dependency with no source change.
---

# Rebuild

3.1. Confirm no source code changes are needed for the package itself, AND confirm the
     updated dependency is actually recompiled into the package at build time rather than
     shipped as a pre-built bundled artifact (see step 2.4). If the vulnerable dependency
     is a pre-built blob the build merely re-ships (e.g. a prebuilt webpack/JS bundle
     tarball, vendored minified JS, precompiled binaries), a Release-bump rebuild will NOT
     pick up the fix — do NOT use "rebuild"; choose "backport" or "not-affected" instead.
3.2. Check dependency readiness — search thoroughly:
* Look for linked Jira issues in fields.issuelinks representing the dependency update
* If no linked issue found, use search_jira_issues to find it. Try JQL queries like:
  - project = RHEL AND summary ~ "<CVE-ID>" AND component != "<this-package>"
  Include fields ["key", "summary", "fixVersions", "status"] in the search
* **CRITICAL — fixVersion validation**: The dependency issue MUST target the same
  RHEL version stream as this issue (same major.minor, e.g. both rhel-10.3 or
  rhel-10.3.z). If the search returns multiple results, select only the one
  whose fixVersion matches this issue's stream. NEVER use a dependency tracker
  from a different stream (e.g. do not use a rhel-10.0.z tracker for a
  rhel-10.3 issue — that is a completely different release).
* Once found (with matching fixVersion), call get_jira_details on the dependency
  issue and thoroughly verify it was actually fixed:
  - Check if 'Fixed in Build' field is set (non-null/non-empty)
  - Check the issue status and resolution — if the dependency issue was
    Closed/Done with resolution like 'NOTABUG', 'WONTFIX', 'DUPLICATE',
    'CANTFIX', or 'DROPPED', the fix was never actually built and the
    rebuild is not needed. In this case use "not-affected" resolution
    with explanation that the dependency fix was dropped/rejected.
* If the dependency issue has `Fixed in Build` set AND was not
  dropped/rejected → resolution is "rebuild"
  Set dependency_issue to the issue key AND dependency_component to the component name
  (e.g., "golang", "openssl") from the dependency issue's component field
* If the dependency issue exists but has no `Fixed in Build` yet
  and is still open → resolution is "postponed_dependency"
  Set summary to explain that rebuild is waiting for the dependency to ship,
  and set pending_issues to the dependency issue key.
  Set blocker_references to a JSON array containing the dependency issue key, e.g. ["RHEL-67890"].
  Also set package, fix_version, cve_id, dependency_issue, and dependency_component
  (same values as you would for a rebuild resolution).
* **Fallback — no matching CVE tracker found**: If no dependency CVE tracker
  exists for this version stream, search for a rebase/update ticket on the
  dependency component that may include the CVE fix (e.g. a golang rebase
  that bumps to a version containing the fix). Use JQL like:
  - project = RHEL AND component = "<dependency>" AND (summary ~ "rebase" OR summary ~ "update") AND fixVersion in ("<fixVersion>", "<fixVersion>.z")
  If a rebase ticket is found, verify with certainty that the rebase target
  version actually includes the fix for this CVE (check upstream release
  notes or changelogs). Only use the rebase ticket as the dependency_issue
  if you are 100% sure it resolves the CVE.
* If you cannot find any dependency ticket for the correct version stream,
  or you are not certain that a found ticket resolves the CVE, use
  "clarification-needed" resolution — do NOT fall back to using a tracker
  from a different version stream.
3.3. You must provide a clear justification explaining why a rebuild is needed
     and how it addresses the issue.
3.4. If rebuild: set Jira fields as per the instructions below.
