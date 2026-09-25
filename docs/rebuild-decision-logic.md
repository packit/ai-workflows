# Rebuild Decision Logic

How Ymir determines whether a package needs to be rebuilt for a CVE dependency fix.

## Decision Flow

1. **Search Jira** for builds with "Fixed in Build" set (closed + active)
   - Closed query failure → `TransientInfrastructureError` (retry)
   - Active query failure → tolerated if closed candidates exist
2. **Select highest EVR** from closed builds via Koji metadata
   - Any Koji lookup failure → `TransientInfrastructureError` (retry)
   - Prevents selecting from an incomplete candidate set
3. **Determine built architectures** from Koji RPM list
   - Noarch-only builds: try common arches, accept any log found
4. **Fetch root.log** for each built architecture
   - 404/410 → log genuinely absent (retention)
   - 5xx/401/403/429 → `TransientInfrastructureError` (retry)
   - Transport errors → `TransientInfrastructureError` (retry)
   - Old builds (>90 days): absent logs fall back to timestamp comparison
   - Recent builds: absent logs → `TransientInfrastructureError` (retry)
5. **Compare dependency versions**:
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

- Fetches root.log only for architectures actually built (from Koji RPM list)
- Noarch builds: fetches from whichever builder arch is available
- Normalizes epoch values (explicit `0:` equals omitted epoch)
- Verifies all architectures agree on dependency version
- Requests clarification on conflicts

## Clarification Reasons

| Reason | When |
|--------|------|
| `built_after_fix_no_rootlog` | Package built after fix but root.log unavailable (Brew retention policy) |
| `architecture_dependency_conflict` | Different architectures show different dependency versions |
| `partial_architecture_coverage` | Dependency missing from some architecture logs |
| `active_builds_not_closed` | Builds in progress exist (might be rejected before closure) |

## Infrastructure Failures

Transient infrastructure failures raise `TransientInfrastructureError` for automatic task retry:
- **Jira query failures**: Closed or active build query timeout/connection error
- **Koji metadata unavailable**: getBuild() failures, listRPMs() errors, partial candidate lookups
- **Brew HTTP errors**: 5xx, 401, 403, 429 when fetching root.log
- **Transport errors**: Connection failures, timeouts when fetching root.log

Tasks are retried automatically instead of requesting user clarification.
