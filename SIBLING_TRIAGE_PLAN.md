# Sibling Triage Plan

## Problem
Currently, sibling consolidation uses simplified LLM analysis that might incorrectly consolidate NOT_AFFECTED issues. Users want full triage analysis for each sibling.

## Critical Constraint
**Primary's MR cannot be updated after it's merged/closed.** Therefore, we MUST wait for all siblings to finish triaging before starting the primary's rebase. This ensures the MR includes all REBASE siblings at creation time.

## Solution: Wait for Siblings + Single Consolidation

### Labels Used
- `ymir_rebase_sibling` - Marks a sibling queued for triage
- `ymir_waiting_for_siblings` - Marks primary waiting for siblings to triage

### Phase 1: Queue Siblings (During Primary Issue Triage)

When triaging the primary issue and it resolves to REBASE:

1. Find sibling candidates using existing `build_rebase_siblings_jql()`
2. If siblings found:
   - For each candidate:
     - Check eligibility (`check_cve_triage_eligibility`)
     - If eligible: push to triage queue
     - Add label `ymir_rebase_sibling` to sibling
     - Post comment on sibling: "Queued for triage as potential sibling of {primary_issue}"
   - Add label `ymir_waiting_for_siblings` to primary
   - Post comment on primary: "Waiting for {count} siblings to finish triaging before starting rebase"
   - **Do NOT queue primary for rebase yet**
3. If no siblings found:
   - Queue primary for rebase immediately as before

**Files to modify:**
- `ymir/agents/rebase_consolidation.py`: Add `queue_siblings_for_triage()`
- `ymir/agents/triage_agent.py`: Call it after REBASE decision, conditionally queue primary
- `ymir/common/constants.py`: Add `JiraLabels.REBASE_SIBLING` and `JiraLabels.WAITING_FOR_SIBLINGS`

### Phase 2: Check if Ready to Queue Primary (When Sibling Finishes Triaging)

When any issue with `ymir_rebase_sibling` label finishes triaging:

1. Remove `ymir_rebase_sibling` label
2. Add appropriate triage label (`ymir_triaged_rebase`, `ymir_triaged_not_affected`, etc.)
3. Find primary issue by reading the sibling's comment
4. Check if all siblings have finished triaging:
   - Query for siblings of primary that still have `ymir_rebase_sibling` label
   - If count == 0: all siblings are done!
5. If all siblings done:
   - Remove `ymir_waiting_for_siblings` from primary
   - Queue primary for rebase
   - Comment on primary: "All siblings triaged, starting rebase"

**Files to modify:**
- `ymir/agents/triage_agent.py`: After triage completes, check if primary can be queued
- `ymir/agents/rebase_consolidation.py`: Add `check_and_queue_primary_if_ready()`

### Phase 3: Consolidate at Primary Rebase

When primary issue's rebase workflow starts:

1. Search JQL: siblings with `ymir_triaged_rebase` for same component+fixVersion
2. For each, fetch RebaseData and compare target version
3. If version matches: add to consolidated_issues list
4. Proceed with consolidated rebase MR as before
5. All REBASE siblings are in the MR from the start

**Files to modify:**
- `ymir/agents/rebase_consolidation.py`: Create `find_triaged_rebase_siblings()`
- Replace current `find_rebase_siblings()` call with new function

### Phase 4: Label Cleanup

After consolidation:
- Remove `ymir_rebase_sibling` from any siblings (should already be removed)
- Add `ymir_rebased` to all consolidated siblings

**Files to modify:**
- `ymir/agents/rebase_agent.py`: Label cleanup in success path (already does this)

## Flow Diagram

```
Primary Issue Triages → REBASE
  ├─→ Find sibling candidates
  ├─→ If siblings found:
  │     ├─→ Queue each sibling for triage
  │     ├─→ Label siblings with ymir_rebase_sibling
  │     ├─→ Comment on siblings: "Queued as sibling of {primary}"
  │     ├─→ Label primary with ymir_waiting_for_siblings
  │     ├─→ Comment on primary: "Waiting for {N} siblings"
  │     └─→ **Do NOT queue primary for rebase**
  └─→ If no siblings found:
        └─→ Queue primary for rebase immediately

Time passes... siblings triage in parallel

Sibling 1 Finishes Triaging
  ├─→ Remove ymir_rebase_sibling label
  ├─→ Add triage result label (e.g., ymir_triaged_rebase)
  ├─→ Read comment to find primary
  ├─→ Check: are all siblings done? Query for remaining ymir_rebase_sibling
  └─→ Not yet (Sibling 2 still triaging)

Sibling 2 Finishes Triaging
  ├─→ Remove ymir_rebase_sibling label
  ├─→ Add triage result label (e.g., ymir_triaged_not_affected)
  ├─→ Read comment to find primary
  ├─→ Check: are all siblings done? Query for remaining ymir_rebase_sibling
  ├─→ YES! All done!
  ├─→ Remove ymir_waiting_for_siblings from primary
  ├─→ Comment on primary: "All siblings triaged, starting rebase"
  └─→ Queue primary for rebase

Primary Rebase Starts
  ├─→ Find siblings with ymir_triaged_rebase for same package+version
  ├─→ Found: Sibling 1 (matches version!)
  ├─→ Not found: Sibling 2 (was NOT_AFFECTED, not consolidated)
  ├─→ Consolidate primary + Sibling 1 into one MR
  └─→ MR created with all REBASE siblings included
```

## Implementation Order

1. Add `REBASE_SIBLING` and `WAITING_FOR_SIBLINGS` labels to constants
2. Create `queue_siblings_for_triage()` in rebase_consolidation.py
   - Label siblings with `ymir_rebase_sibling`
   - Comment on siblings and primary
   - Return sibling count
3. Create `check_and_queue_primary_if_ready()` in rebase_consolidation.py
   - Extract primary from comment
   - Query for remaining `ymir_rebase_sibling` siblings
   - Queue primary if all done
4. Create `find_triaged_rebase_siblings()` in rebase_consolidation.py
   - Search for `ymir_triaged_rebase` siblings
   - Compare target versions
5. Update triage_agent.py:
   - After REBASE decision, call queue_siblings_for_triage()
   - Only queue primary if no siblings OR not waiting
   - After any triage completes, call check_and_queue_primary_if_ready()
6. Update rebase workflow to use find_triaged_rebase_siblings()
7. Test with dotnet issues

## Benefits

1. **Accuracy**: Each sibling gets full triage (version check, CVE applicability)
2. **Correctness**: NOT_AFFECTED siblings won't be consolidated
3. **Transparency**: Each sibling has its own triage decision
4. **Single MR**: All REBASE siblings in one MR from the start
5. **No closed MR problem**: Primary waits so MR never needs updating after merge
6. **Clean**: Simple linear flow, no bidirectional complexity

## Edge Cases

- **What if a sibling triage errors/fails?** It gets `ymir_triage_errored` label, still counts as "done triaging"
- **What if no siblings end up being REBASE?** Primary rebases alone, that's fine
- **What if primary is re-triaged while waiting?** Check if still has `ymir_waiting_for_siblings`, clear it if resolution changed
- **Timeout?** Could add a timeout (e.g., 24 hours) after which primary proceeds anyway
