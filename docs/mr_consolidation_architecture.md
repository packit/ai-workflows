# MR Consolidation Agent — Architecture and Design

## Problem Statement

When multiple CVE fixes or backports are filed for the same package and branch, each
backport agent run creates a separate merge request. Package maintainers then have to
manually combine these MRs, resolve spec file conflicts (Patch tag numbering, Release
bumps, `%prep` ordering), and adapt patches that touch overlapping source files so they
apply cleanly in sequence. This is tedious, error-prone, and blocks merge velocity.

The MR Consolidation Agent automates this: it picks up pairs of backport MRs, combines
their changes into a single coherent MR, verifies the result builds, and marks the
originals as consolidated.

## High-Level Architecture

```
┌──────────────┐       submit_merge_job()       ┌────────────────────┐
│              │  ─────────────────────────────▶ │                    │
│   Backport   │   (after filing an MR, if       │   Redis Queue      │
│   Agent      │    consolidation.json enables   │   (Hash-based,     │
│              │    it for the package)           │    per pkg/branch) │
└──────────────┘                                 └────────┬───────────┘
                                                          │
┌──────────────┐       submit_merge_job()                 │
│ Jira Issue   │  ─────────────────────────────▶          │
│ Fetcher      │   (when ymir_consolidate_base            │
│              │    + _next labels match)                  │
└──────────────┘                                          │
                                                 pick_next_job()
                                                 (Lua-atomic)
                                                          │
                                                          ▼
                                                 ┌────────────────────┐
                                                 │  MR Consolidation  │
                                                 │  Agent (worker)    │
                                                 │                    │
                                                 │  1. list_open_mrs  │
                                                 │  2. fork & clone   │
                                                 │  3. filter by HEAD │
                                                 │  4. LLM merges     │
                                                 │  5. build verify   │
                                                 │  6. log & commit   │
                                                 │  7. mark originals │
                                                 │  8. update Jira    │
                                                 │  9. requeue if >2  │
                                                 └────────────────────┘
```

## Redis Queue Design

### Data Structure

A single Redis **Hash** (`merge_consolidation_queue`) stores all consolidation jobs.
Each package-branch pair has up to two fields:

| Field pattern              | Meaning                                     |
|----------------------------|---------------------------------------------|
| `{pkg}:{branch}:pending`  | A job waiting to be picked up               |
| `{pkg}:{branch}:active`   | A job currently being processed by a worker |

**Invariant**: At most one `pending` and one `active` entry exist per package-branch
pair at any time.

### Operations

**`submit_merge_job(package, branch)`** — Called by the backport agent after successfully
filing an MR. Creates a `pending` entry. If one already exists, it's a no-op (the
existing pending job will pick up the new MR when it runs).

**`pick_next_job()`** — Finds any `pending` field whose package-branch pair has no
`active` field, atomically deletes the `pending` entry and creates an `active` entry
with the same value. Implemented as a **Lua script** running inside Redis, so the
scan-check-promote is a single atomic operation — multiple concurrent workers cannot
pick the same job.

```lua
-- Scans all fields, finds a :pending without a matching :active,
-- atomically promotes it.
local hash = KEYS[1]
local fields = redis.call('HGETALL', hash)
for i = 1, #fields, 2 do
    local field = fields[i]
    if string.sub(field, -8) == ':pending' then
        local prefix = string.sub(field, 1, #field - 8)
        local active_key = prefix .. ':active'
        if redis.call('HEXISTS', hash, active_key) == 0 then
            redis.call('HDEL', hash, field)
            redis.call('HSET', hash, active_key, fields[i + 1])
            return {field, fields[i + 1]}
        end
    end
end
return nil
```

**`complete_job(package, branch)`** — Deletes the `active` field after the workflow
finishes (success or failure), freeing the slot for the next pending job.

### Why a Hash Instead of a List/Stream?

- **Deduplication**: The key structure naturally prevents duplicate jobs for the same
  package-branch pair.
- **At-most-one-active guarantee**: A worker checks for an existing `active` key
  before promoting, all within a single Lua script.
- **Visibility**: `HGETALL` gives a full snapshot of all in-flight work.

## Workflow Steps

### Step 1: `list_open_mrs`

Queries the GitLab API for open MRs on the target project/branch with the
`ymir_backport` label, sorted by creation date (oldest first). MRs already labeled
`ymir_consolidated` are excluded. If fewer than 2 eligible MRs remain, the workflow
exits with a no-op success status.

If branch info is pre-populated (e.g., in tests or direct invocation with
`backport_branches`), this step is skipped entirely.

### Step 2: `fork_and_prepare_dist_git`

Forks the upstream dist-git repository (or reuses an existing fork), clones it locally,
and checks out the target branch. Then fetches all candidate MR branches into the local
clone.

#### Stale MR Filtering

After fetching branches, the workflow resolves the current HEAD of the target branch
and runs `git merge-base <target_branch> <mr_branch>` for each candidate MR. If the
merge-base does not equal the target branch HEAD, the MR is **stale** — it was created
against an older version of the branch. Stale MRs are logged with a warning and
excluded from consolidation.

This prevents merging MRs that are out of date. Responsibility for rebasing a stale
backport onto the current HEAD remains with the backport agent (or a manual rebase).

Only the two oldest non-stale MRs are selected for consolidation.

### Step 3: `run_consolidation_agent`

An LLM agent (BeeAI `ReasoningAgent`) is given the local clone, both source branches,
and detailed instructions for combining the changes. The agent:

1. **Examines** both branches to understand the changes (spec file diffs, new patch
   files, overlapping source files).
2. **Orders patches by size** — the physically larger patch gets a lower Patch number
   so it is applied first. This minimizes the adaptation work needed for the smaller
   patch.
3. **Adapts overlapping patches** — if two patches modify the same source files, the
   later patch's context lines must be updated to match the post-first-patch source.
   The agent uses `run_package_prep` to produce the intermediate source tree and
   regenerates or fixes the later patch accordingly.
4. **Verifies** all patches apply without fuzz warnings and builds an SRPM.

If the agent reports failure, the workflow transitions to `handle_failure`.

### Step 4: `run_build_agent`

A separate build agent verifies the SRPM produced by the consolidation agent actually
builds in the target build system (Koji/Brew scratch build). If the build fails, the
workflow loops back to `run_consolidation_agent` with the build error message, up to
`max_build_attempts` times (default 3). This mirrors the retry pattern used by the
backport agent.

### Step 5: `stage_changes`

Stages all modified files (`git add -A`) in the local clone.

### Step 6: `run_log_agent`

An LLM agent generates the changelog entry and commit message for the consolidated
changeset, summarizing all merged branches and resolved Jira issues.

### Step 7: `commit_push_and_open_mr`

Squashes all commits into a single commit with the generated log message, pushes to
the fork, and opens a new consolidated MR against the target branch. The consolidated
MR is labeled `ymir_backport` so it can be discovered by subsequent consolidation runs.

In dry-run mode, this step skips the push and MR creation.

### Step 8: `mark_original_mrs`

Labels the two original MRs with `ymir_consolidated` so they are excluded from future
consolidation runs. The MRs remain **open** — they are not closed — so maintainers can
still review and reference them. In dry-run mode, this is logged but not executed.

### Step 9: `update_jira_issues`

Posts a comment on every Jira issue involved in the consolidation, linking the new
consolidated MR. Without this, a maintainer would have no pointer from the Jira issue
to the consolidated replacement. In dry-run mode, this is logged but not executed.

### Step 10: `requeue_if_needed`

If more than 2 non-stale MRs were found in step 2, the workflow submits a new
consolidation job to the Redis queue. The next run will find the newly created
consolidated MR (labeled `ymir_backport`) plus the remaining originals — it excludes
any MR already labeled `ymir_consolidated` — and merges the next pair.

This creates a chain: 4 MRs → consolidate 2 → requeue → consolidate result + 3rd →
requeue → consolidate result + 4th → done. Each step reduces the count by one until
all MRs are folded into a single comprehensive MR.

**No infinite loop risk**: The requeue count is based on the number of non-stale MRs
found after HEAD-filtering. Each successful consolidation marks 2 MRs as
`ymir_consolidated` (excluding them from future searches) and creates 1 new MR,
reducing the eligible count by 1. The workflow only requeues when `count - 2 >= 1`,
i.e., when 3+ MRs were present.

## Per-Package Configuration

Consolidation is enabled by default for all packages. The backport agent reads
`consolidation.json` from the per-package rules repository
(`gitlab.com/redhat/centos-stream/rules/<package>/consolidation.json`) before
submitting a consolidation job.

```json
{
  "merge_mrs": true,
  "release_strategy": "per_commit"
}
```

| Field              | Default          | Description                                             |
|--------------------|------------------|---------------------------------------------------------|
| `merge_mrs`        | `true`           | Enable/disable MR consolidation for this package        |
| `release_strategy` | `"per_commit"`   | `"per_commit"`: separate commit per original MR, each bumping Release by 1 (base→base+1, base+1→base+2). `"merged"`: single Release bump (+1 from base, all patches in one commit) |

If the file is absent or unreadable, consolidation runs with defaults (enabled, `per_commit` strategy).

## Running the Agent

### Standalone (direct mode)

For manual testing against a real package:

```bash
make run-mr-consolidation-agent-standalone \
    PACKAGE=expat \
    BRANCH=rhel-9.8.0 \
    DRY_RUN=true
```

In direct mode, the agent queries GitLab for open MRs on the specified package/branch,
runs the consolidation workflow once, and exits. No Redis connection is used, so the
requeue step logs a warning but does not submit follow-up jobs.

### Queue mode (production)

When started without `PACKAGE`/`BRANCH` env vars, the agent enters a polling loop on
the Redis queue, picking up jobs submitted by backport agents.

### E2E tests

```bash
make run-mr-consolidation-agent-e2e-tests DRY_RUN=true
```

The E2E test suite runs backport agents (or replays cached backport patches) to create
backport branches in a mock bare repository, then runs the consolidation workflow
against them, and verifies:

- Both patches are present in the consolidated changeset
- The spec file has correct Patch tags and `%prep` lines
- An SRPM builds successfully
- An LLM-as-a-judge evaluation confirms the consolidated patches still fix the
  original CVEs

## Triggering Modes

### Auto mode (backport-triggered)

After filing a backport MR, the backport agent calls `submit_merge_job(package,
branch)`. The consolidation worker picks up the job, queries GitLab for all open
backport MRs on that package/branch, and consolidates the two oldest non-stale ones.

### Label-triggered mode

Maintainers can request consolidation of two **specific** MRs by adding Jira labels:

1. Add `ymir_consolidate_base` to the Jira issue whose MR should be the base.
2. Add `ymir_consolidate_next` to another issue (same package/branch) whose MR should
   be merged on top.

The `JiraIssueFetcher` periodic scan detects the pair, matches them by component and
`fixVersions`-derived branch, and calls `submit_merge_job(package, branch,
source_issues=[base_key, next_key])`. It then removes both labels and posts a comment
on each issue confirming submission.

When the consolidation worker picks up a job with `source_issues` set, it resolves
each Jira key to its GitLab MR (by searching MR titles/descriptions) and consolidates
exactly those two MRs — it does **not** fall back to the "oldest two" heuristic.

**Unmatched labels**: If a `ymir_consolidate_base` issue has no corresponding
`ymir_consolidate_next` for the same package/branch (or vice versa), the fetcher logs
a warning and leaves the labels untouched for the next sweep.

## Safety Invariants

| Invariant | Mechanism |
|-----------|-----------|
| No duplicate jobs per package-branch | Redis Hash key structure + `submit_merge_job` checks |
| No concurrent processing of same package-branch | Lua script atomic promote in `pick_next_job` |
| No stale MR merging | `git merge-base` check against current target branch HEAD |
| No infinite requeue loop | Requeue only when `non_stale_count - 2 >= 1`; each consolidation reduces count by 1 |
| Build verification before MR creation | Build agent with retry loop, same pattern as backport agent |
| No re-consolidation of consumed MRs | Original MRs are labeled `ymir_consolidated` and filtered out of future searches |
| No accidental real writes in tests | `DRY_RUN=true` skips push, MR creation, MR labeling, and Jira comments |
| Label pair atomicity | Fetcher removes both consolidation labels and posts comments only after successful job submission |
| Jira traceability | `update_jira_issues` posts consolidated MR URL to every involved Jira issue |

## File Map

| File | Purpose |
|------|---------|
| `ymir/agents/mr_consolidation_agent.py` | Workflow orchestration, state machine |
| `ymir/common/merge_queue.py` | Redis queue functions (`submit_merge_job`, `pick_next_job`, `complete_job`) |
| `ymir/agents/prompts/mr_consolidation/instructions.j2` | LLM agent instructions (patch ordering, adaptation, verification) |
| `ymir/agents/prompts/mr_consolidation/prompt.j2` | LLM agent prompt template (MR details, retry context) |
| `ymir/common/models.py` | Pydantic schemas (`MergeConsolidationJob`, `PackageConsolidationConfig`, I/O schemas) |
| `ymir/common/constants.py` | `MERGE_CONSOLIDATION_QUEUE` Redis key name |
| `ymir/tools/privileged/gitlab.py` | `ListProjectMergeRequestsTool`, `AddMergeRequestLabelsTool` |
| `ymir/agents/backport_agent.py` | `submit_consolidation_job` step (triggers consolidation) |
| `ymir/jira_issue_fetcher/jira_issue_fetcher.py` | Label-triggered consolidation scanning (`_process_consolidation_labels`) |
| `ymir/agents/tests/unit/test_mr_consolidation.py` | Unit tests for Redis queue logic |
| `ymir/agents/tests/e2e/mr_consolidation/` | E2E test suite with LLM judge evaluation |
