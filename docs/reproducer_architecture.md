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
lock so sibling-stream workers serialize the full reproducer run (analysis,
TF verification, and MR push).

## High-Level Architecture

```
┌──────────────┐   AUTO_CHAIN (parallel)    ┌────────────────────┐
│              │  ─────────────────────────▶│  rebase / backport │
│   Triage     │                            │  / rebuild queues  │
│   Agent      │                            └────────────────────┘
│              │   TRIAGE_ENQUEUE_REPRODUCER (parallel)
│              │  ─────────────────────────▶┌────────────────────┐
│              │   rebase|backport|rebuild  │  reproducer_queue  │
│              │   |not-affected            │  (+ _todo twin)    │
└──────────────┘                            └─────────┬──────────┘
                                                      │
                                            BRPOP / delayed promote
                                                      │
                                                      ▼
                                            ┌────────────────────┐
                                            │  Queue orchestration │
                                            │  (per task)          │
                                            │  • package gate      │
                                            │  • acquire lock      │
                                            │  • in_progress label │
                                            └─────────┬──────────┘
                                                      │ lock held
                                                      ▼
                                            ┌────────────────────┐
                                            │  Reproducer workflow │
                                            │                    │
                                            │  0. Bootstrap tests│
                                            │     clone + MR tip │
                                            │  1. LLM analysis   │
                                            │     (TF verify /    │
                                            │      reuse / adapt)│
                                            │  2. create/update  │
                                            │     MR             │
                                            │  3. Jira labels +  │
                                            │     comment        │
                                            └────────────────────┘
                                                      │
                         ┌────────────────────────────┼────────────────────────────┐
                         ▼                            ▼                            ▼
              ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
              │ tests repo MR    │      │ Testing Farm     │      │ Redis lock hash  │
              │ ymir_reproducer  │      │ reserve / run /  │      │ + blocked lists  │
              │                  │      │ cancel           │      │ per package:lock │
              └──────────────────┘      └──────────────────┘      └──────────────────┘
```

Triage still sends fix work to rebase/backport/rebuild when `AUTO_CHAIN=true`.
The reproducer job is an **additional** parallel enqueue when triage
auto-enqueue is enabled (see [Triggering](#triggering)).

## Package Enablement

Before enqueue (triage) or before running (queue worker), orchestration reads
the `reproducer` section of `ymir.yaml` from
`gitlab.com/redhat/centos-stream/rules/<package>` via `fetch_reproducer_config`.

| Config | Behavior |
|--------|----------|
| File missing or no `reproducer` key | Default: **disabled** |
| `reproducer.enabled: true` | Proceed |
| `reproducer.enabled: false` | Skip silently (no workflow, no terminal reproducer label) |
| Malformed `reproducer` section | Skip; at queue time post a Jira error comment asking maintainers to fix `ymir.yaml` |

Triage enqueue also skips when config is disabled or invalid. Manual
`make trigger-reproducer` bypasses triage enqueue but the queue worker still
checks config before acquiring the lock.

## Triggering

### Auto-enqueue from triage

> **Enabled by default** (`TRIAGE_ENQUEUE_REPRODUCER=true` in
> `openshift/configmap-agents-env.yml` and local `compose.yaml`). Set
> `TRIAGE_ENQUEUE_REPRODUCER=false` to disable triage LPUSH and submit jobs
> manually with `make trigger-reproducer` (see
> [Manual queue submit](#manual-queue-submit) below).

`TRIAGE_ENQUEUE_REPRODUCER` is **independent** of `AUTO_CHAIN`. Fix-agent
enqueue uses `AUTO_CHAIN`; reproducer enqueue uses its own flag.

When `TRIAGE_ENQUEUE_REPRODUCER` is true and triage resolves as one of:

| Resolution       | Fix agent queued? (`AUTO_CHAIN`) | Reproducer eligible? |
|------------------|----------------------------------|----------------------|
| `rebase`         | Yes (if `AUTO_CHAIN`)            | Yes                  |
| `backport`       | Yes (if `AUTO_CHAIN`)          | Yes                  |
| `rebuild`        | Yes (if `AUTO_CHAIN`)          | Yes                  |
| `not-affected`   | No                               | Yes                  |
| `postponed`      | No (postponed list)              | No                   |
| `clarification-needed` / `open-ended-analysis` / `error` | Special | No |

Additional gates before LPUSH:

1. `_build_reproducer_input` must resolve `package` on the resolution data.
2. `fetch_reproducer_config` must return `enabled: true`.

Triage builds a flat `ReproducerInputSchema` (not full `TriageState`) and
`lpush`es a `Task` to `reproducer_queue` or `reproducer_queue_todo` when
`user_triggered`.

For `not-affected`, applicability overrides copy `package` / `cve_id` /
`fix_version` / `triage_summary` / `patch_urls` from the prior resolution, and
the N/A explanation is folded into `triage_summary` for the reproducer prompt.

### Standalone / queue / test modes

| Mode | Purpose | How |
|------|---------|-----|
| Standalone | Manual testing of one issue (no Redis) | `make run-reproducer-agent-standalone JIRA_ISSUE=… DRY_RUN=…` |
| Queue | Production worker | `reproducer-agent` compose service without `JIRA_ISSUE` — BRPOPs queues |
| E2E tests | Automated regression suite (mock repos, `dry_run=True`) | `make run-reproducer-agent-e2e-tests` |

For ad-hoc queue testing, you can also manually Redis-`lpush` a `Task` whose
`metadata` validates as `ReproducerInputSchema`.

Standalone mode runs `run_workflow` once with no Redis: no queue lock, no
duplicate guard, and no package gate unless `PACKAGE` is passed via queue
metadata (standalone with only `JIRA_ISSUE` leaves `package` unset unless the
agent resolves it from Jira).

## Redis Queues and Retries

| Key | Type | Role |
|-----|------|------|
| `reproducer_queue` | List | Normal reproducer tasks |
| `reproducer_queue_todo` | List | Priority twin (`ymir_todo` / user-triggered) |
| `reproducer_queue_delayed` | ZSET | Deferred retries (score = unix ready-time) |
| `reproducer_blocked:{package}:{lock_id}` | List | Tasks waiting for the create/adapt lock |
| `completed_reproducer_list` | List | Finished non-retry outcomes |
| `reproducer_creation_lock` | Hash | Active lock entries (`{package}:{lock_id}:active`) |

The worker BRPOPs `[reproducer_queue_todo, reproducer_queue]`. Each poll cycle
also:

1. `sweep_stale_reproducer_locks()` — clear abandoned locks (default 6h) and
   promote any tasks on matching blocked lists
2. `promote_due_tasks()` — move ready delayed payloads back onto the
   appropriate list (`_todo` if `user_triggered`)

**Delayed ZSET retries** (`reproducer_queue_delayed`) are used only for
**retryable infra** (`retryable_error`, e.g. Testing Farm provisioning). Default
delay: `REPRODUCER_RETRY_DELAY_SECONDS` (1800s).

**Lock contention** does **not** use the delayed ZSET. A worker that cannot
acquire the lock parks the task on `reproducer_blocked:{package}:{lock_id}` via
`enqueue_blocked_reproducer_task`. When the holder releases the lock (or stale
sweep removes it), `promote_blocked_reproducer_tasks` LPUSHes blocked payloads
back to `reproducer_queue` or `reproducer_queue_todo` (preserving
`user_triggered`). The blocked waiter still receives
`ymir_reproducer_in_progress` and a user ack when `user_triggered`.

After `MAX_RETRIES` failed queue attempts (uncaught exception or missing package),
the worker sets `ymir_reproducer_errored` and pushes to `error_list`.

The legacy output flag `lock_deferred` is no longer set by the agent;
orchestration blocks at workflow start instead.

## Working Directory Layout

Each run uses a per-issue directory under `GIT_REPO_BASEPATH` (default
`/git-repos`):

```
/git-repos/Reproducer/<JIRA_ISSUE>/tests-<package>/
```

`run_workflow` removes and recreates `Reproducer/<JIRA_ISSUE>/` at the start of
each run. Orchestration bootstraps the tests clone at
`tests-<package>` before the LLM runs (see below). SCP allowlisting permits paths
under `/git-repos` and `/tmp`; remote tools scope copies to the job's Reproducer
tree via MCP metadata (`jira_issue`, `package`).

## Cross-Stream Reuse and Adapt

The tests repository is **shared across streams**. Package conventions vary;
CVE tests are often under `Security/<CVE>/` and bug tests under
`Regression/<JIRA>/`, but the agent may use a different relative path. The
agent MUST return that path as `test_directory` in its output; orchestration
never invents or guesses the location.

### Orchestration bootstrap

Before the LLM runs (when `package` is known), orchestration:

1. Clones `https://gitlab.com/redhat/rhel/tests/<package>` into the working
   directory.
2. Lists open MRs with label `ymir_reproducer`.
3. Matches an open MR for this CVE or Jira issue (see [MR matching](#mr-matching) below).
4. If matched: fetches and checks out the MR source branch; discovers
   `existing_test_directory` on that branch.
5. Passes bootstrap context into the prompt (`tests_clone_ready`,
   `tests_clone_path`, `existing_mr_url`, `mr_source_branch`,
   `existing_test_directory`) so the agent adapts in place and does not
   re-clone over the checked-out branch.

### Agent behavior (prompt)

When bootstrap did not run, the agent clones via `clone_repository` and follows
the prompt workflow:

1. Look for an existing test directory and grep for issue/CVE references.
2. List open MRs with label `ymir_reproducer` via `list_project_merge_requests`.
3. **If found:** reserve Testing Farm for **this** stream’s compose and run the
   existing test.
   - Works → `success=true`, `test_already_exists=true`,
     `adapted_existing=false` (no new MR); still set `test_directory`.
   - Fails on this stream → adapt the test in the **same directory path** on the
     open MR branch, re-verify, set `adapted_existing=true`, `existing_mr_url`,
     and `test_directory`.
4. **If not found:** create a new test and set `test_directory` to its relative
   path under the clone.

The agent does not set `test_mr_url` or `lock_deferred`; orchestration owns MR
URLs and lock scheduling.

### MR matching

Open reproducer MRs are matched by **MR title only** using canonical bracket
tags (descriptions are ignored):

| Reproducer type | Title pattern | Cross-stream behavior |
|-----------------|---------------|------------------------|
| CVE | `package: [CVE-…] ymir reproducer test` | One MR per CVE set (stable across streams) |
| Regression (bug) | `package: [RHEL-…] ymir reproducer test` | Sibling streams extend the same MR; title accumulates keys e.g. `[RHEL-100, RHEL-200]` |

Matching order: explicit `existing_mr_url` from agent output, then title CVE
tags, then title Jira tags (issue key or **Cloners-chain root** for regression
siblings). `_match_regression_sibling_mr` handles clone-chain grouping.

### Orchestration (`create_merge_request`)

Uses `result.test_directory` (relative to the tests clone) as the sole source of
truth for which files to `git add`. Rejects absolute paths and `..` segments.

| Result flags | Action |
|--------------|--------|
| `test_already_exists` and not `adapted_existing` | Skip MR |
| `adapted_existing` and success | Push to existing MR source branch (or `reproducer/<jira>` fallback) |
| New success | Commit `test_directory`, fork, open MR with label `ymir_reproducer` |
| `retryable_error` | Skip MR; do not write terminal Jira labels (delayed retry) |
| Missing/invalid `test_directory` when MR needed | Fail; skip MR |

When bootstrap found `existing_test_directory` and the agent reports
`adapted_existing`, `test_directory` must match that path; otherwise MR creation
is skipped.

For **new** MRs, orchestration may call `request_mr_qe_reviews` when
`target_branch` or `fix_version` resolves a dist-git branch and
`ASSIGN_MR_REVIEWERS=true`.

Orchestration GitLab tools (not in the agent MCP allowlist): `get_merge_request_details`,
`fetch_branch`, `fork_repository`, `commit_push_and_open_mr` (via `tasks.py`).

### not-affected semantics

Triage N/A rationale is passed in `triage_summary`. The agent still attempts a
test that would detect the issue **if present**:

- Bug detected on this compose → report clearly (contradicts N/A).
- Not reproducible after iteration limit → supports N/A
  (`not_reproducible_reason` / summary).

## Multi-Worker Clash Prevention (Redis Lock)

Sibling issues for the same CVE (different streams) or regression clones in the
same Cloners chain contend on one lock so only one worker runs the full
reproducer workflow (analysis, TF, MR) at a time.

### Lock id

Resolved by `resolve_reproducer_lock_id`:

| Job type | `lock_id` |
|----------|-----------|
| CVE (`cve_id` set) | Normalized CVE id(s): sorted, comma-joined if multiple |
| Bug (no CVE) | Root issue of the Jira **Cloners** chain (Y-stream root), via issuelinks; falls back to issue key if resolution fails |

See `reproducer_lock_id()` and `resolve_clone_root()`.

### Data structures

Redis **Hash** `reproducer_creation_lock`:

| Field pattern | Meaning |
|---------------|---------|
| `{package}:{lock_id}:active` | Worker currently holding the workflow lock |

Per-lock **blocked list** `reproducer_blocked:{package}:{lock_id}` — tasks that
could not acquire the lock (not scanned by the Jira fetcher's queue dedup; see
[Fetcher stale recovery](#fetcher-stale-recovery)).

Unlike MR consolidation, there is **no pending slot** in the hash — waiters park
on the blocked list until release or stale sweep.

### Lifecycle (queue mode)

1. **`try_acquire_reproducer_lock(package, lock_id)`** at the start of
   `process_task` (before `run_workflow`). Lua `HEXISTS` then `HSET` if absent.
   Returns an ownership token (serialized `ReproducerLockEntry` JSON) on success,
   or `None` if busy.
2. On success: set `ymir_reproducer_in_progress`, run the full workflow.
3. **`release_reproducer_lock(package, lock_id, token)`** in `finally` after the
   workflow completes. Compare-and-delete the `:active` field only when *token*
   still matches. Then `promote_blocked_reproducer_tasks` for that lock.
4. On busy: stage in-progress label (and user ack if triggered), RPUSH task to
   blocked list, return without running the workflow.

**`sweep_stale_reproducer_locks(threshold=6h)`** — Removes stale `:active`
entries and promotes blocked tasks for each swept lock, using compare-and-delete
so a re-acquired lock is not wiped.

### Covered race

rhel-10 finishes and opens the MR; later rhel-9 and rhel-8 both need to adapt
the same CVE MR. The same `package:cve` lock serializes full runs. Waiters sit
on the blocked list until rhel-10 releases; they then re-bootstrap the tests
clone (often already on the MR branch), re-verify, and often exit as reuse-only
after the first portable adaptation lands.

### Standalone mode

Queue locking lives in `process_task`, not inside `run_workflow`. Standalone /
direct mode (`JIRA_ISSUE` env, no Redis consumer) never acquires the lock.

## Workflow Steps

### Step 0: Bootstrap (orchestration, before LLM)

See [Orchestration bootstrap](#orchestration-bootstrap) (`_bootstrap_tests_clone`).
Skipped when `package` is unset.

### Step 1: `run_reproducer_analysis`

BeeAI `ReasoningAgent` with tools for Jira, patches, maintainer rules, git
clone, Testing Farm reserve/details/cancel, remote copy/run, and
`list_project_merge_requests`. Prompt:
`ymir/agents/prompts/reproducer/prompt.j2`.

**Context management:** the agent may call `manage_context` in the same turn as
another tool to replace older dead-end tool traffic with an agent-authored
`durable_summary`. The system prompt is rebuilt each turn (outside memory) and
the task `UserMessage` is meta-protected, so neither is compacted. Compaction is
deferred until after parallel tools finish — no extra inference round.

`TFReservationCleanupMiddleware` tracks reservations and cancels leaks in
`finally` even if the agent fails to call cancel.

### Step 2: `create_merge_request`

See orchestration table above. Commits under
`GIT_REPO_BASEPATH/Reproducer/<jira_issue>/tests-<package>`, forks the tests
project, opens or updates the MR via `commit_push_and_open_mr`.

### Step 3: `handle_results`

Writes terminal Jira labels and a comment unless `retryable_error` (keeps
`ymir_reproducer_in_progress` for delayed retry).

| Outcome | Label |
|---------|-------|
| New or adapted success | `ymir_reproducer_created` |
| Existing verified, no adapt | `ymir_reproducer_already_exists` |
| Not reproducible | `ymir_reproducer_not_reproducible` |
| Other failure | `ymir_reproducer_failed` |
| Exhausted queue retries | `ymir_reproducer_errored` |

**Queue dedup:** before work, skip if a terminal reproducer label is present and
the issue is not in-progress (unless `user_triggered`). Staging in-progress
removes terminal reproducer labels so a fresh run can replace them.

## Jira Labels and Fetcher

| Label | Role |
|-------|------|
| `ymir_reproducer_in_progress` | Dedup anchor while running; in fetcher `IN_FLIGHT_LABELS` for stale recovery |
| `ymir_reproducer_created` / `_already_exists` / `_not_reproducible` / `_failed` / `_errored` | Terminal outcomes |
| GitLab `ymir_reproducer` | Marks tests-repo MRs for discovery |

Triage still stamps its own terminal labels (`ymir_triaged_*`). The reproducer
runs afterward without clearing those. Fetcher stale recovery for
`ymir_reproducer_in_progress` therefore ignores coexisting triage/fix-agent
labels and only treats another `ymir_reproducer_*` label as proof the stage
already finished.

### Fetcher stale recovery

When `ymir_reproducer_in_progress` looks abandoned (no Jira update within
`STALE_LABEL_THRESHOLD_HOURS`, default 24h) and the issue is not already queued
in `reproducer_queue` / `reproducer_queue_todo`, the fetcher flips the label to
`ymir_retry_needed` and re-enqueues to **triage** (full re-triage). The
original `ReproducerInputSchema` payload only existed in Redis and is not
recoverable from Jira alone.

Blocked tasks on `reproducer_blocked:*` are **not** included in the fetcher's
`existing_keys` scan. A task blocked on lock for longer than the stale threshold
could theoretically be treated as abandoned while still parked (edge case).

See also [jira_label_workflow_routing.md](../jira_label_workflow_routing.md).

## Environment Variables

| Variable | Default | Role |
|----------|---------|------|
| `TRIAGE_ENQUEUE_REPRODUCER` | `true` | Triage LPUSH to reproducer queues |
| `AUTO_CHAIN` | `true` | Triage → fix-agent queues (separate from reproducer) |
| `REPRODUCER_RETRY_DELAY_SECONDS` | `1800` | Delayed ZSET retry for `retryable_error` |
| `REPRODUCER_POLL_TIMEOUT` | `30` | BRPOP timeout (seconds) |
| `MAX_RETRIES` | `3` | Queue task retries before `ymir_reproducer_errored` |
| `MAX_CONCURRENT_TASKS` | `1` | Reproducer worker concurrency |
| `STALE_LABEL_THRESHOLD_HOURS` | `24` | Fetcher abandoned in-flight detection |
| `ASSIGN_MR_REVIEWERS` | `true` (OpenShift) | QE reviewer on new test MRs |
| `GIT_REPO_BASEPATH` | `/git-repos` | Per-issue Reproducer working dirs |
| `DRY_RUN` | `false` | Skip MR and Jira finalization |

## Safety Invariants

| Invariant | Mechanism |
|-----------|-----------|
| No duplicate triage→reproducer for ineligible resolutions | `_REPRODUCER_ELIGIBLE_RESOLUTIONS` gate |
| No enqueue without package | `_build_reproducer_input` returns `None` |
| No run for disabled packages | `fetch_reproducer_config` + `enabled: true` |
| One full reproducer run at a time per package+lock_id | Redis lock at `process_task` start; release in `finally` |
| Waiters do not spin on lock busy | Blocked list; promoted on release or stale sweep |
| Abandoned locks do not block forever | 6h stale sweep with compare-and-delete |
| No second concurrent run of same Jira issue | `ymir_reproducer_in_progress` + terminal labels (+ queue dedup) |
| No TF machine leaks | Agent cancel + `TFReservationCleanupMiddleware` |
| Remote SSH/SCP cannot target arbitrary hosts | `ssh_host` allowlisted from reservation details |
| SCP paths scoped | Under `/git-repos` or `/tmp`; job metadata scopes Reproducer tree |
| No real writes in dry-run / tests | `DRY_RUN` skips MR and Jira finalization |
| Adapt targets the right MR | Title-only bracket-tag match; bootstrap checks out MR branch before agent runs |
| Adapt uses correct test path | `test_directory` must match `existing_test_directory` when adapting open MR |

## Running and Testing the Agent

### Standalone (manual testing)

For manual testing against a real Jira issue. Prefer `DRY_RUN=true` so the
workflow does not open tests-repo MRs or write Jira labels/comments:

```bash
make run-reproducer-agent-standalone JIRA_ISSUE=RHEL-12345 DRY_RUN=true
```

In this mode Redis is unused: the agent runs `run_workflow` once and exits.
No queue lock or package gate (unless you pass full metadata via a custom
integration). For realistic runs, use queue mode with `make trigger-reproducer`
or LPUSH a full `ReproducerInputSchema` task.

### E2E tests

Automated end-to-end tests under `ymir/agents/tests/e2e/reproducer_agent/`.
They exercise the workflow against mock repositories and fixtures (not
production Jira/GitLab), with `dry_run=True`:

```bash
make run-reproducer-agent-e2e-tests
```

These are regression tests for the agent — they are not a way to produce
reproducer MRs for real issues.

Unit tests for enqueue helpers, labels, blocked lock, and MR helpers live under
`ymir/agents/tests/unit/` and `ymir/common/tests/unit/` (see File Map).

### Queue mode (production)

Start the compose `reproducer-agent` service (agents profile) with Redis and
**without** `JIRA_ISSUE`. With default settings, triage auto-enqueues eligible
resolutions to the reproducer queues. Use `make trigger-reproducer` (below) for
ad-hoc jobs or when `TRIAGE_ENQUEUE_REPRODUCER=false`.

On OpenShift, `openshift/deployment-reproducer-agent.yml` runs the same queue
worker (`beeai-agent:c10s`, module `ymir.agents.reproducer_agent`). Testing Farm
calls go through `mcp-gateway`, which must mount the `testing-farm-env` secret
(`TESTING_FARM_API_TOKEN`). Apply via `./openshift/deploy.sh`, then enqueue and
watch from the `openshift/` directory (requires `oc login`):

```bash
make -C openshift trigger-reproducer JIRA_ISSUE=RHEL-12345 PACKAGE=bind
make -C openshift show-reproducer-queue
make -C openshift logs-reproducer
```

Same optional flags as the local target (`CVE_ID`, `FIX_VERSION`,
`TARGET_BRANCH`, `TRIAGE_SUMMARY`, `USER_TRIGGERED`).

### Manual queue submit

For ad-hoc enqueue or when `TRIAGE_ENQUEUE_REPRODUCER=false`. With the agents
stack running (`make start` / `make start DRY_RUN=true`) and the
`reproducer-agent` worker up, enqueue a job:

```bash
# Minimum (required fields)
make trigger-reproducer JIRA_ISSUE=RHEL-12345 PACKAGE=bind

# Typical CVE job
make trigger-reproducer \
  JIRA_ISSUE=RHEL-12345 \
  PACKAGE=bind \
  CVE_ID=CVE-2025-12345 \
  FIX_VERSION=rhel-10.1 \
  TARGET_BRANCH=c10s \
  TRIAGE_SUMMARY='Triage concluded backport; verify on compose before adapting.'

# Priority queue (reproducer_queue_todo) — posts user-facing ack comments
make trigger-reproducer JIRA_ISSUE=RHEL-12345 PACKAGE=bind USER_TRIGGERED=true
```

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `JIRA_ISSUE` | yes | — | Issue key (e.g. `RHEL-12345`) |
| `PACKAGE` | yes | — | Downstream component / tests-repo name |
| `CVE_ID` | no | unset | CVE id(s); lock id and Security/ path hints |
| `FIX_VERSION` | no | unset | Jira fix version (e.g. `rhel-10.1`) |
| `TARGET_BRANCH` | no | unset | Dist-git / stream hint (e.g. `c10s`) |
| `TRIAGE_SUMMARY` | no | unset | Free-text context for the reproducer prompt |
| `USER_TRIGGERED` | no | `false` | `true` → `reproducer_queue_todo` + user ack comments |

`ReproducerInputSchema` also supports `patch_urls` (set by triage enqueue, not
exposed by `make trigger-reproducer`). For manual jobs, LPUSH JSON with full
metadata if patch context is needed.

The target LPUSHes a `Task` whose `metadata` validates as
`ReproducerInputSchema` onto Valkey. Watch progress with:

```bash
$(COMPOSE) -f compose.yaml --profile=agents logs -f reproducer-agent
# or Redis Commander at http://localhost:8081/
```

Standalone mode bypasses Redis entirely and is better for quick prompt/dry-run
experiments without a running worker.

## File Map

| File | Purpose |
|------|---------|
| `ymir/agents/triage_agent.py` | `_build_reproducer_input`, `_enqueue_reproducer`, `TRIAGE_ENQUEUE_REPRODUCER` gate |
| `ymir/agents/reproducer_agent.py` | Workflow, queue consumer, bootstrap, MR create/adapt, lock integration |
| `ymir/agents/tasks.py` | `fetch_reproducer_config`, `commit_push_and_open_mr`, `request_mr_qe_reviews` |
| `ymir/agents/reasoning_agent/context_management.py` | `manage_context` tool + deferred memory compaction |
| `ymir/common/reproducer_lock.py` | Acquire / release / stale sweep / blocked queue promote |
| `ymir/common/delayed_queue.py` | Delayed retry ZSET helpers |
| `ymir/common/models.py` | `ReproducerInputSchema`, `ReproducerOutputSchema`, `PackageReproducerConfig` |
| `ymir/common/constants.py` | Queue names, `get_reproducer_queue()`, Jira labels |
| `ymir/agents/prompts/reproducer/prompt.j2` | LLM workflow (reuse / verify / adapt) |
| `ymir/agents/prompts/reproducer/output_format.j2` | Expected agent JSON |
| `ymir/agents/tf_cleanup_middleware.py` | TF reservation leak cleanup |
| `ymir/tools/privileged/testing_farm.py` | TF MCP tools, SSH/SCP allowlisting |
| `ymir/tools/privileged/gitlab.py` | Fork, MR, branch fetch (orchestration) |
| `ymir/jira_issue_fetcher/jira_issue_fetcher.py` | `IN_FLIGHT` includes reproducer; stale → triage re-queue |
| `agents_as_skills/reproducer/SKILL.md` | Skill mirror of the workflow |
| `ymir/agents/tests/unit/test_reproducer_agent.py` | Unit tests: labels, MR helpers, blocked lock |
| `ymir/agents/tests/unit/test_context_management.py` | Unit tests: manage_context compaction |
| `ymir/agents/tests/unit/test_triage_agent.py` | Unit tests: enqueue input builder |
| `ymir/common/tests/unit/test_reproducer_lock.py` | Unit tests: create/adapt lock + blocked queue |
| `ymir/agents/tests/e2e/reproducer_agent/` | E2E test suite (mock repos / fixtures) |
| `openshift/deployment-reproducer-agent.yml` | Production OpenShift Deployment |
| `openshift/configmap-agents-env.yml` | `TRIAGE_ENQUEUE_REPRODUCER`, `ASSIGN_MR_REVIEWERS`, etc. |
