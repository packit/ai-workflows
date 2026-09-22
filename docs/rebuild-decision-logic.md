# Rebuild Decision Logic

How Ymir determines whether a package needs to be rebuilt for a CVE dependency fix.

## Decision Flow

1. **Search Jira** for builds with "Fixed in Build" set (closed + active)
2. **Select highest EVR** from closed builds
3. **Fetch root.log** from all architectures to verify dependency version
   - Root.log may not be accessible for older builds (Brew retention policy)
   - If unavailable, fall back to timestamp comparison
4. **Compare dependency versions**:
   - **Root.log available**: Compare EVRs (used vs fixed)
     - Used >= Fixed → Already Fixed
     - Used < Fixed → Check for active builds, else Rebuild
   - **Root.log unavailable**: Compare build timestamps
     - Built before fix → Check for active builds, else Rebuild
     - Built after fix → Clarification (cannot prove fix without root.log)

## Active Build Handling

Before returning "rebuild needed", checks if active (in-progress) builds exist:
- If active builds exist → Clarification (might be newer rebuild in progress)
- Otherwise → Rebuild

## Multi-Architecture Verification

- Fetches root.log from x86_64, aarch64, ppc64le, s390x
- Normalizes epoch values (explicit `0:` equals omitted epoch)
- Verifies all architectures agree on dependency version
- Requests clarification on conflicts

## Clarification Reasons

| Reason | When |
|--------|------|
| `built_after_fix_no_rootlog` | Package built after fix but root.log unavailable |
| `architecture_dependency_conflict` | Different architectures show different dependency versions |
| `partial_architecture_coverage` | Dependency missing from some architecture logs |
| `active_builds_not_closed` | Builds in progress exist (might be rejected before closure) |
| `timestamp_comparison_failed` | Koji metadata unavailable for timestamp comparison |
| `evr_comparison_failed` | Koji metadata unavailable for EVR comparison |
| `jira_query_failed` | Active build query failed and no closed builds found |

## Metadata Failures

When Koji metadata unavailable (connection timeout, listRPMs failure):
- Returns clarification, not "rebuild needed"
- Missing metadata is indeterminate, not proof rebuild is needed
- Preserves closed build results even if active query fails
