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

- **`ymir_rebase_sibling`**: Sibling queued for triage, terminal until triage completes
  - Added by primary during sibling queueing (Phase 1)
  - Removed by triage agent when sibling finishes triaging (Phase 2)
  - Acts as dedup anchor: prevents fetcher from re-queueing siblings
  - Not included in `IN_FLIGHT_LABELS` (no stale recovery for siblings)

- **`ymir_rebase_waiting_for_siblings`**: Primary waiting for siblings to finish triaging
  - Added when primary queues siblings and waits (Phase 1)
  - Removed when all siblings finish triaging (Phase 2)
  - Makes `ymir_triaged_rebase` non-terminal: allows re-triaging after siblings finish
  - Critical label: removal uses retry with backoff (3 attempts)

- **`ymir_triaged_rebase`**: Terminal triage label, except when waiting for siblings
  - Added after successful REBASE triage
  - When `ymir_rebase_waiting_for_siblings` is present: non-terminal (allows re-queue)
  - When `ymir_rebase_waiting_for_siblings` is absent: terminal (dedup anchor)
  - Removed during re-triage after siblings finish

### Three-Phase Workflow

#### Phase 1: Queue Siblings (Primary Triage)

When primary issue triages as REBASE:
1. Find sibling candidates (same package + fixVersion, not already triaged)
2. For each eligible candidate:
   - Check CVE eligibility (skip if not IMMEDIATELY)
   - Check for terminal labels (skip if already processed)
   - Queue for triage in Redis
   - Add `ymir_rebase_sibling` label
   - Post comment: "Queued for triage as potential sibling of {primary}" (with smartlink)
3. If any siblings queued:
   - Add `ymir_rebase_waiting_for_siblings` label to primary
   - Post comment listing all queued siblings (clickable issue links)
   - Skip adding `ymir_triaged_rebase` (added later during re-triage)
   - Skip rebase queue (primary waits)
4. If no siblings queued:
   - Add `ymir_triaged_rebase` label immediately
   - Queue primary for rebase

**Function**: `queue_siblings_for_triage()` in `rebase_consolidation.py`

**Comment format**:
```
Waiting for 3 sibling(s) to finish triaging before starting rebase:
RHEL-234827, RHEL-234375, RHEL-224663
```

#### Phase 2: Check Primary Ready (Sibling Finishes)

When a sibling finishes triaging:
1. Remove `ymir_rebase_sibling` label from sibling
2. Add resolution label to sibling (e.g., `ymir_triaged_rebase`)
3. Extract primary issue key from sibling's comments (looks for "potential sibling of {primary}")
4. Query primary's current labels and check for:
   - `ymir_rebase_waiting_for_siblings` must be present
   - No other issues with `ymir_rebase_sibling` referencing this primary
5. If all siblings done:
   - Remove `ymir_rebase_waiting_for_siblings` from primary (critical operation, 3 retries)
   - Re-queue primary to triage queue (not rebase queue)
   - Primary runs full triage again (LLM + eligibility + version check)
   - Triage removes `ymir_triaged_rebase` when it sees sibling wait state
   - Triage adds `ymir_triaged_rebase` back after re-analysis
   - Triage queues primary to rebase queue with full state
   - Post comment: "All siblings have finished triaging, starting rebase"
6. If siblings still pending:
   - Do nothing, wait for next sibling to finish

**Function**: `check_and_queue_primary_if_ready()` in `rebase_consolidation.py`
**Called from**: `triage_agent.py` after triage completes (if sibling label present)

**Why re-triage?**: Rebase queue requires full `TriageState` in `Task.metadata`. Re-triaging
ensures primary has complete state for rebase workflow.

#### Phase 3: Consolidate (Primary Rebase)

When primary rebase starts:
1. Validate primary is still open (skip if Closed/Done/Resolved)
2. Search for siblings with `ymir_triaged_rebase` label (same package + fixVersion)
3. For each candidate:
   - Fetch issue details including comments
   - Extract text from ADF/HTML comment bodies (handles smartlink tags)
   - Check for exact match: "Queued for triage as potential sibling of {primary}"
   - If found: include in consolidated list
4. Create MR with primary + all consolidated siblings
5. MR description includes all consolidated issue keys

**Function**: `find_triaged_rebase_siblings()` in `rebase_consolidation.py`
**Called from**: `rebase_agent.py` in `find_consolidated_siblings` step

**Primary validation**: Prevents creating MRs that reference closed/resolved primary issues
when rebase workflow runs from stale Redis messages.

### Why Comment-Based Matching?

Phase 3 uses comment checking instead of version comparison because:
- **Simpler**: No need to fetch RebaseData or compare versions
- **More efficient**: Comments already exist from Phase 1
- **More reliable**: Direct reference instead of version inference
- **Race-proof**: Comment is immutable, written once during queueing
- **Handles ADF/HTML**: Extracts issue keys from smartlink tags in MCP responses

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
        state.rebase_waiting_for_siblings = True
        # Skip adding ymir_triaged_rebase (added during re-triage)
        # Clear consolidated_issues - populated in Phase 3
        # Primary waits
    else:
        state.rebase_waiting_for_siblings = False
        # Add ymir_triaged_rebase immediately
        # Queue primary for rebase

# Phase 2: Check if ready (after triage completes)
if ymir_rebase_sibling in labels:
    remove ymir_rebase_sibling label
    await check_and_queue_primary_if_ready(...)
        # If all siblings done: re-queues primary to triage

# Skip terminal label if waiting for siblings
if not (state.rebase_waiting_for_siblings or ymir_rebase_waiting_for_siblings in labels):
    add resolution_label  # e.g., ymir_triaged_rebase

# Skip rebase queue if waiting for siblings
if state.rebase_waiting_for_siblings or ymir_rebase_waiting_for_siblings in labels:
    skip queueing to rebase

# Remove ymir_rebase_waiting_for_siblings during re-triage
if ymir_rebase_waiting_for_siblings in labels and not waiting_for_any_siblings:
    remove ymir_rebase_waiting_for_siblings label
    # This happens during re-triage after all siblings finish
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

**Sibling triage fails**: Counted as "done" (no `ymir_rebase_sibling` label), primary proceeds
when others finish, failed sibling not consolidated.

**No REBASE siblings**: Primary queues when last sibling finishes, rebases alone.

**Package already at target version**: Triage returns REBASE but rebase errors "version not older
than target". Workaround: manually set "Fixed in Build" on all issues. Future: new resolution type.

**Primary manually closed while waiting**: Primary validation in Phase 3 checks if primary is
Closed/Done/Resolved and skips consolidation (prevents broken MR links).

**Sibling already triaged as different resolution**: Sibling queuing skips issues with terminal
labels (`ymir_triaged_backport`, `ymir_triaged_not_affected`, etc.) to avoid re-triaging completed work.

**Comment body with smartlink tags**: `extract_text_from_adf()` handles both ADF JSON and HTML
smartlink tags (`<custom data-type="smartlink">`) by extracting issue keys from URLs.

**Critical label removal fails**: `ymir_rebase_waiting_for_siblings` removal uses `critical=True`
flag → 3 retry attempts with exponential backoff. If all fail, primary remains waiting (no infinite loop).

**Timeout**: Sibling hangs → primary waits forever. No timeout implemented yet.
Future: timeout (e.g., 24h) to force-queue primary.

## Benefits

- **Accurate consolidation**: Each sibling gets full triage (version check, CVE applicability)
- **Single MR**: All REBASE siblings included from start, no updates needed
- **Reduced volume**: 15 CVEs → 1 MR (example)
- **Easier review**: One MR per rebase instead of duplicates
- **Better traceability**: All issues linked in one place

## Deployment

**Prerequisites**: None (no DB migrations)

**Monitoring**:
- Issues with `ymir_rebase_waiting_for_siblings` (should clear when all siblings finish)
- Issues with `ymir_rebase_sibling` (should clear when sibling triages)
- Sibling comments with smartlink extraction failures
- Primary validation failures (closed primaries with waiting siblings)
- Critical label removal failures (retries exhausted)
- Multiple issues per rebase MR (consolidation working)
- Reduction in MR count (measure consolidation effectiveness)
