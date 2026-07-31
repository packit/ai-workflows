# Rebase MR Consolidation

**Jira Issue**: [PACKIT-5186](https://redhat.atlassian.net/browse/PACKIT-5186)

**Objective**: Group rebase merge requests by stream instead of creating one MR per Jira issue.

## Problem

Multiple CVEs targeting the same component and stream result in duplicate MRs that rebase the same package to the same version.

**Example**: dotnet10.0 / rhel-10.2.z had 11 separate issues requiring rebase to the same version.

## Solution

A **wait-for-siblings** workflow that:
1. Queues all sibling issues for full triage analysis
2. Waits for all siblings to finish triaging
3. Creates a single MR with all REBASE siblings included from the start

**Critical constraint**: MRs cannot be updated after merge/close, so all siblings must finish triaging before the primary starts its rebase.

## Architecture

### Labels

- **`ymir_rebase_sibling`**: Issue queued for triage as potential sibling
- **`ymir_waiting_for_siblings`**: Primary waiting for siblings to finish

### Three-Phase Workflow

#### Phase 1: Queue Siblings (Primary Triage)

When primary issue triages as REBASE:
1. Find sibling candidates (same package + fixVersion, not already triaged)
2. For each candidate:
   - Check CVE eligibility
   - Queue for triage
   - Add `ymir_rebase_sibling` label
   - Post comment: "Queued for triage as potential sibling of {primary}"
3. Add `ymir_waiting_for_siblings` label to primary
4. Skip rebase queue (primary waits)

**Function**: `queue_siblings_for_triage()` in `rebase_consolidation.py`

#### Phase 2: Check Primary Ready (Sibling Finishes)

When a sibling finishes triaging:
1. Remove `ymir_rebase_sibling` label
2. Add resolution label (e.g., `ymir_triaged_rebase`)
3. Extract primary issue from comments
4. Check if any siblings still have `ymir_rebase_sibling` label
5. If all done:
   - Remove `ymir_waiting_for_siblings` from primary
   - Queue primary for rebase
   - Post comment: "All siblings triaged, starting rebase"

**Function**: `check_and_queue_primary_if_ready()` in `rebase_consolidation.py`
**Called from**: `triage_agent.py` after triage completes

#### Phase 3: Consolidate (Primary Rebase)

When primary rebase starts:
1. Search for siblings with `ymir_triaged_rebase` label (same package + fixVersion)
2. For each candidate:
   - Check comments for "Queued for triage as potential sibling of {primary}"
   - If found: include in consolidated list
3. Create MR with primary + all consolidated siblings

**Function**: `find_triaged_rebase_siblings()` in `rebase_consolidation.py`
**Called from**: `rebase_agent.py` in `find_consolidated_siblings` step

### Why Comment-Based Matching?

Phase 3 uses comment checking instead of version comparison because:
- **Simpler**: No need to fetch RebaseData or compare versions
- **More efficient**: Comments already exist from Phase 1
- **More reliable**: Direct reference instead of version inference
- **Race-proof**: Comment is immutable

## Implementation

### Files Modified

- `ymir/common/constants.py`: Added labels
- `ymir/agents/rebase_consolidation.py`: Core functions
- `ymir/agents/triage_agent.py`: Phase 1 and Phase 2 integration
- `ymir/agents/rebase_agent.py`: Phase 3 integration
- `ymir/common/models.py`: RebaseData fields (already existed)

### Code Flow

**Triage Agent**:
```python
# Phase 1: Queue siblings
consolidate_rebase_siblings():
    sibling_count = await queue_siblings_for_triage(...)
    if sibling_count > 0:
        # Clear consolidated_issues - populated in Phase 3
        # Primary waits
    else:
        # No siblings - queue primary immediately

# Phase 2: Check if ready
# After triage completes:
if ymir_rebase_sibling in labels:
    remove label
    await check_and_queue_primary_if_ready(...)

# Skip rebase queue if waiting
if ymir_waiting_for_siblings in labels:
    skip queueing
```

**Rebase Agent**:
```python
# Phase 3: Find siblings
find_consolidated_siblings():
    if not state.consolidated_issues:
        included, summary = await find_triaged_rebase_siblings(...)
        state.consolidated_issues = included
        state.consolidation_summary = summary
```

## Edge Cases

**Sibling triage fails**: Counted as "done" (no `ymir_rebase_sibling` label), primary proceeds when others finish, failed sibling not consolidated.

**No REBASE siblings**: Primary queues when last sibling finishes, rebases alone.

**Package already at target version**: Triage returns REBASE but rebase errors "version not older than target". Workaround: manually set "Fixed in Build" on all issues. Future: new resolution type.

**Primary re-triaged while waiting**: Label remains, may cause error when queued. Future: check resolution before queueing.

**Timeout**: Sibling hangs → primary waits forever. Future: timeout (e.g., 24h) to force-queue primary.

## Benefits

- **Accurate consolidation**: Each sibling gets full triage (version check, CVE applicability)
- **Single MR**: All REBASE siblings included from start, no updates needed
- **Reduced volume**: 15 CVEs → 1 MR (example)
- **Easier review**: One MR per rebase instead of duplicates
- **Better traceability**: All issues linked in one place

## Deployment

**Prerequisites**: None (no DB migrations)

**Monitoring**:
- Issues with `ymir_waiting_for_siblings` (should clear quickly)
- Issues with `ymir_rebase_sibling` (should clear when sibling triages)
- Multiple issues per rebase MR
- Reduction in MR count
