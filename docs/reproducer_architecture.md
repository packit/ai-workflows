# Reproducer Agent — Architecture and Design

## Problem Statement

RHEL bugs and CVEs need automated, objective reproducers in
`gitlab.com/redhat/rhel/tests/<package>` so maintainers and CI can verify
fixes. Writing those tests by hand is slow, and doing it independently per
stream creates waste and conflict:

- Triage may conclude **not-affected**, but a real test is still valuable to
  confirm the issue is absent on that compose.
- Sibling issues for the same CVE on rhel-10, rhel-9, and rhel-8 all map to
  the **same** tests-repo path (`Security/<CVE>/`). Two workers that each open
  an MR with a similar test leave maintainers with duplicates.
- A test written for one stream may not run correctly on another; the second
  stream should **verify** and, if needed, **adapt** the existing MR rather
  than invent a parallel test.

The Reproducer Agent automates this: after triage, it designs or reuses a
tmt/BeakerLib test, verifies it on Testing Farm for the issue’s stream, opens
or updates a single tests-repo MR labeled `ymir_reproducer`, and uses a Redis
lock so sibling-stream workers serialize create/adapt work.

## High-Level Architecture

```
┌──────────────┐   AUTO_CHAIN (parallel)    ┌────────────────────┐
│              │  ─────────────────────────▶│  rebase / backport │
│   Triage     │                            │  / rebuild queues  │
│   Agent      │                            └────────────────────┘
│              │   AUTO_CHAIN (parallel)
│              │  ─────────────────────────▶┌────────────────────┐
│              │   rebase|backport|rebuild  │  reproducer_queue  │
│              │   |not-affected            │  (+ _todo twin)    │
└──────────────┘                            └─────────┬──────────┘
                                                      │
                                            BRPOP / delayed promote
                                                      │
                                                      ▼
                                            ┌────────────────────┐
                                            │  Reproducer Agent  │
                                            │                    │
                                            │  1. LLM analysis   │
                                            │     (TF verify /   │
                                            │      reuse / adapt)│
                                            │  2. create/update  │
                                            │     MR (under lock)│
                                            │  3. Jira labels +  │
                                            │     comment        │
                                            └────────────────────┘
                                                      │
                         ┌────────────────────────────┼────────────────────────────┐
                         ▼                            ▼                            ▼
              ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
              │ tests repo MR    │      │ Testing Farm     │      │ Redis lock hash  │
              │ ymir_reproducer  │      │ reserve / run /  │      │ package:lock_id  │
              │                  │      │ cancel           │      │ :active          │
              └──────────────────┘      └──────────────────┘      └──────────────────┘
```

Triage still sends fix work to rebase/backport/rebuild as before. The
reproducer job is an **additional** parallel enqueue for eligible resolutions.

## Triggering

### Auto-chain from triage

When `AUTO_CHAIN=true` (default) and triage resolves as one of:

| Resolution       | Fix agent queued? | Reproducer queued? |
|------------------|-------------------|--------------------|
| `rebase`         | Yes               | Yes                |
| `backport`       | Yes               | Yes                |
| `rebuild`        | Yes               | Yes                |
| `not-affected`   | No                | Yes                |
| `postponed`      | No (postponed list) | No               |
| `clarification-needed` / `open-ended-analysis` / `error` | Special | No |

Triage builds a flat `ReproducerInputSchema` (not full `TriageState`) via
`_build_reproducer_input` and `lpush`es a `Task` to `reproducer_queue` or
`reproducer_queue_todo` when `user_triggered`.

Required for enqueue: `package` on the resolution data. For `not-affected`,
applicability overrides copy `package` / `cve_id` / `fix_version` /
`triage_summary` / `patch_urls` from the prior resolution, and the N/A
explanation is folded into `triage_summary` for the reproducer prompt.

### Standalone / queue / test modes

| Mode | Purpose | How |
|------|---------|-----|
| Standalone | Manual testing of one issue (no Redis) | `make run-reproducer-agent-standalone JIRA_ISSUE=… DRY_RUN=…` |
| Queue | Production worker | `reproducer-agent` compose service without `JIRA_ISSUE` — BRPOPs queues |
| E2E tests | Automated regression suite (mock repos, `dry_run=True`) | `make run-reproducer-agent-e2e-tests` |

For ad-hoc queue testing, you can also manually Redis-`lpush` a `Task` whose
`metadata` validates as `ReproducerInputSchema`.

## Redis Queues and Delayed Retries

| Key | Type | Role |
|-----|------|------|
| `reproducer_queue` | List | Normal reproducer tasks |
| `reproducer_queue_todo` | List | Priority twin (`ymir_todo` / user-triggered) |
| `reproducer_queue_delayed` | ZSET | Deferred retries (score = unix ready-time) |
| `completed_reproducer_list` | List | Finished non-retry outcomes |

The worker BRPOPs `[reproducer_queue_todo, reproducer_queue]`. Each poll cycle
also:

1. `sweep_stale_reproducer_locks()` — clear abandoned create/adapt locks
2. `promote_due_tasks()` — move ready delayed payloads back onto the
   appropriate list (`_todo` if `user_triggered`)

Delayed retries are used for:

- **Retryable infra** (`retryable_error`, e.g. Testing Farm provisioning)
- **Lock contention** (`lock_deferred` — another worker holds create/adapt)

Default delay: `REPRODUCER_RETRY_DELAY_SECONDS` (1800s).

## Cross-Stream Reuse and Adapt

The tests repository is **shared across streams**. Package conventions vary;
CVE tests are often under `Security/<CVE>/` and bug tests under
`Regression/<JIRA>/`, but the agent may use a different relative path. The
agent MUST return that path as `test_directory` in its output; orchestration
never invents or guesses the location.

### Agent behavior (prompt)

1. Clone `https://gitlab.com/redhat/rhel/tests/<package>` (default branch).
2. Look for an existing test directory and grep for issue/CVE references.
3. List open MRs with label `ymir_reproducer` via
   `list_project_merge_requests`.
4. **If found:** reserve Testing Farm for **this** stream’s compose and run
   the existing test.
   - Works → `success=true`, `test_already_exists=true`,
     `adapted_existing=false` (no new MR); still set `test_directory`.
   - Fails on this stream → adapt the test to be portable across streams,
     re-verify, set `adapted_existing=true`, `existing_mr_url`, and
     `test_directory` to the adapted path.
5. **If not found:** create a new test and set `test_directory` to its
   relative path under the clone.

### Orchestration (`create_merge_request`)

Uses `result.test_directory` (relative to the tests clone) as the sole
source of truth for which files to `git add`. Rejects absolute paths and
`..` segments.

| Result flags | Action |
|--------------|--------|
| `test_already_exists` and not `adapted_existing` | Skip MR |
| `adapted_existing` and success | Acquire lock; push to existing MR source branch (or `reproducer/<jira>` fallback) |
| New success | Acquire lock; commit `test_directory`, fork, open MR with label `ymir_reproducer` |
| `lock_deferred` / `retryable_error` | Skip MR; do not write terminal Jira labels |
| Missing/invalid `test_directory` when MR needed | Fail; skip MR |

Branch resolution for adapt: list open `ymir_reproducer` MRs and match by
`existing_mr_url` or CVE/issue text in title/description.

### not-affected semantics

Triage N/A rationale is passed in `triage_summary`. The agent still attempts a
test that would detect the issue **if present**:

- Bug detected on this compose → report clearly (contradicts N/A).
- Not reproducible after iteration limit → supports N/A
  (`not_reproducible_reason` / summary).

## Multi-Worker Clash Prevention (Redis Lock)

Sibling issues for the same CVE (different streams) contend on one lock so
only one worker creates or adapts the canonical test/MR at a time.

### Data structure

Redis **Hash** `reproducer_creation_lock`:

| Field pattern | Meaning |
|---------------|---------|
| `{package}:{lock_id}:active` | Worker currently creating or adapting |

**Lock id:** normalized CVE id(s) (sorted, comma-joined if multiple) or, for
non-CVE bugs, the Jira issue key. See `reproducer_lock_id()`.

Unlike MR consolidation, there is **no pending slot** — waiters requeue on
`reproducer_queue_delayed` and retry later.

### Operations

**`try_acquire_reproducer_lock(package, lock_id)`** — Lua `HEXISTS` then
`HSET` if absent. Returns whether this caller holds the lock.

**`release_reproducer_lock(package, lock_id)`** — `HDEL` the `:active` field
(always in `finally` after create/adapt push, except cancel paths that leave
the lock for the stale sweep).

**`sweep_stale_reproducer_locks(threshold=6h)`** — Removes `:active` entries
whose `activated_at` is older than the threshold, using compare-and-delete so
a lock released and re-acquired between snapshot and delete is not wiped.

### Covered race

rhel-10 finishes and opens the MR; later rhel-9 and rhel-8 both find the test
fails on their compose and would adapt the same MR. The same `package:cve`
lock serializes adapters. The loser delayed-retries, re-clones / re-lists MRs,
re-verifies first, and often exits as reuse-only after the first portable
adaptation lands.

### When the lock is required

| Path | Lock? |
|------|-------|
| Create new MR | Yes |
| Adapt and push to existing MR | Yes |
| Pure verify of existing test (no mutate) | No |

Standalone / direct mode (`redis_conn is None`) skips the lock.

## Workflow Steps

### Step 1: `run_reproducer_analysis`

BeeAI `ReasoningAgent` with tools for Jira, patches, maintainer rules, git
clone, Testing Farm reserve/details/cancel, remote copy/run, and
`list_project_merge_requests`. Prompt:
`ymir/agents/prompts/reproducer/prompt.j2`.

`TFReservationCleanupMiddleware` tracks reservations and cancels leaks in
`finally` even if the agent fails to call cancel.

### Step 2: `create_merge_request`

See orchestration table above. Commits under
`/git-repos/tests-<package>`, forks the tests project, opens or updates the
MR via `commit_push_and_open_mr`.

### Step 3: `handle_results`

Writes terminal Jira labels and a comment unless `retryable_error` or
`lock_deferred` (keeps `ymir_reproducer_in_progress` for retry).

| Outcome | Label |
|---------|-------|
| New or adapted success | `ymir_reproducer_created` |
| Existing verified, no adapt | `ymir_reproducer_already_exists` |
| Not reproducible | `ymir_reproducer_not_reproducible` |
| Other failure | `ymir_reproducer_failed` |
| Exhausted retries | `ymir_reproducer_errored` |

Dedup: before work, skip if a terminal reproducer label is present and the
issue is not in-progress (unless `user_triggered`).

## Jira Labels and Fetcher

| Label | Role |
|-------|------|
| `ymir_reproducer_in_progress` | Dedup anchor while running; in fetcher `IN_FLIGHT_LABELS` for stale recovery |
| `ymir_reproducer_created` / `_already_exists` / `_not_reproducible` / `_failed` / `_errored` | Terminal outcomes |
| GitLab `ymir_reproducer` | Marks tests-repo MRs for discovery |

Triage still stamps its own terminal labels (`ymir_triaged_*`). The reproducer
runs afterward without clearing those.

See also [jira_label_workflow_routing.md](../jira_label_workflow_routing.md).

## Safety Invariants

| Invariant | Mechanism |
|-----------|-----------|
| No duplicate triage→reproducer for ineligible resolutions | `_REPRODUCER_ELIGIBLE_RESOLUTIONS` gate |
| No enqueue without package | `_build_reproducer_input` returns `None` |
| One create/adapt at a time per package+CVE/issue | Redis lock + Lua acquire |
| Waiters do not spin | Delayed ZSET retry on lock busy |
| Abandoned locks do not block forever | 6h stale sweep with compare-and-delete |
| No second concurrent run of same Jira issue | `ymir_reproducer_in_progress` + terminal labels |
| No TF machine leaks | Agent cancel + `TFReservationCleanupMiddleware` |
| No real writes in dry-run / tests | `DRY_RUN` skips MR and Jira finalization |
| Adapt targets the right MR | Match open `ymir_reproducer` MRs by URL / CVE / issue |

## Running and Testing the Agent

### Standalone (manual testing)

For manual testing against a real Jira issue. Prefer `DRY_RUN=true` so the
workflow does not open tests-repo MRs or write Jira labels/comments:

```bash
make run-reproducer-agent-standalone JIRA_ISSUE=RHEL-12345 DRY_RUN=true
```

In this mode Redis is unused: the agent runs `run_workflow` once and exits.
Create/adapt locking is skipped when there is no Redis connection.

### E2E tests

Automated end-to-end tests under `ymir/agents/tests/e2e/reproducer_agent/`.
They exercise the workflow against mock repositories and fixtures (not
production Jira/GitLab), with `dry_run=True`:

```bash
make run-reproducer-agent-e2e-tests
```

These are regression tests for the agent — they are not a way to produce
reproducer MRs for real issues.

Unit tests for enqueue helpers, labels, and the create/adapt lock live under
`ymir/agents/tests/unit/` and `ymir/common/tests/unit/` (see File Map).

### Queue mode (production)

Start the compose `reproducer-agent` service (agents profile) with Redis and
**without** `JIRA_ISSUE`. Triage (with `AUTO_CHAIN=true`) feeds
`reproducer_queue` / `reproducer_queue_todo`. This is the normal production
path, not a test harness.

## File Map

| File | Purpose |
|------|---------|
| `ymir/agents/triage_agent.py` | `_build_reproducer_input`, `_enqueue_reproducer`, parallel dispatch |
| `ymir/agents/reproducer_agent.py` | Workflow, queue consumer, MR create/adapt, lock integration |
| `ymir/common/reproducer_lock.py` | Acquire / release / stale sweep |
| `ymir/common/delayed_queue.py` | Delayed retry ZSET helpers |
| `ymir/common/models.py` | `ReproducerInputSchema`, `ReproducerOutputSchema`, enriched `NotAffectedData` |
| `ymir/common/constants.py` | Queue names, `get_reproducer_queue()`, Jira labels |
| `ymir/agents/prompts/reproducer/prompt.j2` | LLM workflow (reuse / verify / adapt) |
| `ymir/agents/prompts/reproducer/output_format.j2` | Expected agent JSON |
| `ymir/agents/tf_cleanup_middleware.py` | TF reservation leak cleanup |
| `ymir/tools/privileged/testing_farm.py` | TF MCP tools |
| `ymir/jira_issue_fetcher/jira_issue_fetcher.py` | `IN_FLIGHT` includes reproducer |
| `agents_as_skills/reproducer/SKILL.md` | Skill mirror of the workflow |
| `ymir/agents/tests/unit/test_reproducer_agent.py` | Unit tests: label / MR-need helpers |
| `ymir/agents/tests/unit/test_triage_agent.py` | Unit tests: enqueue input builder |
| `ymir/common/tests/unit/test_reproducer_lock.py` | Unit tests: create/adapt lock |
| `ymir/agents/tests/e2e/reproducer_agent/` | E2E test suite (mock repos / fixtures) |
