# Jira Label-Based Workflow Routing

This document describes how Jira labels control workflow routing through the processing pipeline.

## Label State Machine

```mermaid
stateDiagram-v2
    [*] --> New: Issue Created
    New --> TriagedBackport: ymir_triaged_backport
    New --> TriagedRebase: ymir_triaged_rebase
    New --> Triaged: ymir_triaged
    TriagedBackport --> Success: ymir_backported
    TriagedBackport --> Failed: ymir_backport_failed
    TriagedBackport --> Errored: ymir_backport_errored
    TriagedRebase --> Success: ymir_rebased
    TriagedRebase --> Failed: ymir_rebase_failed
    TriagedRebase --> Errored: ymir_rebase_errored
    Success --> Merged: ymir_merged
    Failed --> Retry: ymir_retry_needed
    Errored --> Attention: ymir_needs_attention
    Retry --> New: Retry Processing
    Merged --> [*]
    Triaged --> [*]
```

## Redis Queue Routing

```mermaid
flowchart TD
    FETCH[Jira Issue Fetcher]

    FETCH -->|No Ymir labels<br/>OR ymir_retry_needed<br/>OR ymir_todo + RH-Employee assignee| TRIAGE[triage_queue]

    TRIAGE --> TRIAGE_AGENT[Triage Agent]

    TRIAGE_AGENT -->|Resolution.REBASE<br/>RHEL 8/9| REBASE_C9S[rebase_queue_c9s]
    TRIAGE_AGENT -->|Resolution.REBASE<br/>RHEL 10+| REBASE_C10S[rebase_queue_c10s]
    TRIAGE_AGENT -->|Resolution.BACKPORT<br/>RHEL 8/9| BACKPORT_C9S[backport_queue_c9s]
    TRIAGE_AGENT -->|Resolution.BACKPORT<br/>RHEL 10+| BACKPORT_C10S[backport_queue_c10s]
    TRIAGE_AGENT -->|Resolution.CLARIFICATION| CLARIFY[clarification_needed_queue]
    TRIAGE_AGENT -->|Resolution.OPEN_ENDED_ANALYSIS| ANALYSIS[open_ended_analysis_list]
    TRIAGE_AGENT -->|Resolution.ERROR| ERROR[error_list]

    REBASE_C9S --> REBASE_AGENT[Rebase Agent]
    REBASE_C10S --> REBASE_AGENT
    BACKPORT_C9S --> BACKPORT_AGENT[Backport Agent]
    BACKPORT_C10S --> BACKPORT_AGENT

    REBASE_AGENT -->|Success| COMPLETED_R[completed_rebase_list]
    REBASE_AGENT -->|Failed/Error| ERROR
    BACKPORT_AGENT -->|Success| COMPLETED_B[completed_backport_list]
    BACKPORT_AGENT -->|Failed/Error| ERROR

    style TRIAGE fill:#fff9c4
    style REBASE_C9S fill:#e1f5fe
    style REBASE_C10S fill:#e1f5fe
    style BACKPORT_C9S fill:#f3e5f5
    style BACKPORT_C10S fill:#f3e5f5
    style ERROR fill:#ffcdd2
    style COMPLETED_R fill:#c8e6c9
    style COMPLETED_B fill:#c8e6c9
```

## Label Reference

### Status Labels

| Label | Added When | Removed When | Next State |
|-------|------------|--------------|------------|
| `ymir_triaged_rebase` | Triage resolves as rebase | On retry (all labels cleared) | `ymir_rebased` or `ymir_rebase_failed` |
| `ymir_triaged_backport` | Triage resolves as backport | On retry (all labels cleared) | `ymir_backported` or `ymir_backport_failed` |
| `ymir_triaged` | Triage resolves as open-ended-analysis | On retry (all labels cleared) | Terminal state |
| `ymir_rebased` | Rebase success | Never | `ymir_merged` |
| `ymir_backported` | Backport success | Never | `ymir_merged` |
| `ymir_merged` | MR merged | Never | Final state |

### Error Labels

| Label | Meaning | Blocks Retry? | Action |
|-------|---------|---------------|--------|
| `ymir_needs_attention` | Human intervention needed | ✅ Yes | Fix issue, remove label, add `ymir_retry_needed` |
| `ymir_triage_errored` | Triage failed | ✅ Yes | Check error_list |
| `ymir_rebase_errored` | Rebase error | ✅ Yes | Check Jira comment |
| `ymir_backport_errored` | Backport error | ✅ Yes | Check Jira comment |
| `ymir_rebase_failed` | Rebase unsuccessful | ❌ No | May auto-retry |
| `ymir_backport_failed` | Backport unsuccessful | ❌ No | May auto-retry |

### Control Labels

| Label | Purpose | Effect |
|-------|---------|--------|
| `ymir_retry_needed` | Trigger retry | Forces reprocessing |
| `ymir_triaged` | Triage completed, no automated follow-up | Terminal state |
| `ymir_fusa` | Functional Safety | Requires maintainer review |
| `ymir_todo` | Maintainer-facing trigger for an e2e run | Fetcher swaps it for `ymir_triage_in_progress` on enqueue; only honored when the assignee is a member of the `Red Hat Employee` Jira group. The triage run posts an ack comment and a result comment so the requester gets feedback. Default is silent — without `ymir_todo`, no comments are posted. |

## Queue Types Summary

| Queue | Type | Triggers | Labels Added | Status |
|-------|------|----------|--------------|--------|
| `triage_queue` | Input | No labels OR `ymir_retry_needed` OR `ymir_todo` | `ymir_triage_in_progress` (set by fetcher atomic flip for retry/todo, or by agent at triage start for fresh issues) | Active |
| `rebase_queue_c9s` | Input | Resolution=REBASE, RHEL 8/9 | `ymir_triaged_rebase` | Active (AUTO_CHAIN only) |
| `rebase_queue_c10s` | Input | Resolution=REBASE, RHEL 10+ | `ymir_triaged_rebase` | Active (AUTO_CHAIN only) |
| `backport_queue_c9s` | Input | Resolution=BACKPORT, RHEL 8/9 | `ymir_triaged_backport` | Active (AUTO_CHAIN only) |
| `backport_queue_c10s` | Input | Resolution=BACKPORT, RHEL 10+ | `ymir_triaged_backport` | Active (AUTO_CHAIN only) |
| `rebase_queue` | Input | (Not actively enqueued) | `ymir_triaged_rebase` | Legacy (checked for deduplication) |
| `backport_queue` | Input | (Not actively enqueued) | `ymir_triaged_backport` | Legacy (checked for deduplication) |
| `clarification_needed_queue` | Input | Resolution=CLARIFICATION | `ymir_needs_attention` | Active (AUTO_CHAIN only) |
| `error_list` | Output | Any error | `ymir_*_errored` | Active |
| `open_ended_analysis_list` | Output | Resolution=OPEN_ENDED_ANALYSIS | `ymir_triaged` | Active (AUTO_CHAIN only) |
| `completed_rebase_list` | Output | Rebase success | `ymir_rebased` | Active |
| `completed_backport_list` | Output | Backport success | `ymir_backported` | Active |

## Deduplication Logic

**Trigger labels are consumed by the fetcher, all other labels by the agent.** The fetcher atomically removes `ymir_todo` and `ymir_retry_needed` before pushing to Redis, replacing them with `ymir_triage_in_progress` so the very next sweep sees the in-progress marker and skips. Every other `ymir_*` label is cleaned up by the triage agent when it pops the task. If the fetcher's atomic flip fails after retries, the Redis push is **skipped** — the issue stays eligible for the next sweep with its trigger label intact, rather than being enqueued without a dedup anchor.

```mermaid
flowchart TD
    START[Jira Issue Fetcher<br/>Found issue]
    INPROG{Has any<br/>ymir_*_in_progress?}
    TODO{Has ymir_todo?<br/>assignee in<br/>Red Hat Employee group}
    RETRY{Has<br/>ymir_retry_needed?}
    OTHER{Has any other<br/>ymir_* label?}

    START --> INPROG
    INPROG -->|Yes| SKIP_RUN[Skip — already running]
    INPROG -->|No| TODO
    TODO -->|Yes| FLIP_TODO[Atomic flip:<br/>+ymir_triage_in_progress<br/>-ymir_todo]
    TODO -->|No| RETRY
    RETRY -->|Yes| FLIP_RETRY[Atomic flip:<br/>+ymir_triage_in_progress<br/>-ymir_retry_needed]
    RETRY -->|No| OTHER
    OTHER -->|Yes| SKIP_DONE[Skip — already processed]
    OTHER -->|No| PUSH_FRESH[Push to triage_queue<br/>user_triggered=False]

    FLIP_TODO --> PUSH_USER[Push to triage_queue<br/>user_triggered=True]
    FLIP_RETRY --> PUSH_RETRY[Push to triage_queue<br/>user_triggered=False]

    style PUSH_FRESH fill:#c8e6c9
    style PUSH_USER fill:#c8e6c9
    style PUSH_RETRY fill:#c8e6c9
    style SKIP_RUN fill:#ffcdd2
    style SKIP_DONE fill:#ffcdd2
```

## Run Behaviour by Trigger and Flag

`DRY_RUN` is the only flag that affects pipeline behaviour. Verbosity is no longer controlled by an env var — the system is silent by default. The only way to opt into comments is per-issue, by adding `ymir_todo` (which flows through the task as `user_triggered=True`).

Ground rules:

- **Default is silent.** No result or error comments are posted on the Jira issue, and intermediate `_failed` labels are not written. Only `not-affected` and `postponed` triage resolutions still post a comment unbidden (those have no MR to look at, so the comment is the only visible explanation).
- **`user_triggered=True`** (set on the task when the issue carried `ymir_todo`) **bypasses every silence filter.** The triage agent posts an immediate private ack comment, posts the result comment, and writes `_failed` labels normally.
- **Labels that are state, not notification, are always written.** `ymir_triage_in_progress` at the start of triage, terminal `ymir_*_errored` / `ymir_triaged_*` at the end. Suppressing them would break dedup against the next fetcher sweep.
- **Jira workflow status is also state.** The rebase and backport agents move the issue to "In Progress" when they pop a task, regardless of `user_triggered`. Triage and the fetcher do not touch the workflow status.
- **`DRY_RUN` is read by both fetcher and agent.** On the fetcher, `DRY_RUN=true` skips the atomic Jira label flip (`ymir_todo` / `ymir_retry_needed` are NOT consumed; `ymir_triage_in_progress` is NOT stamped) but the task is still pushed to Redis with the correct `user_triggered` value, so the agent — also presumably in `DRY_RUN` — can exercise its full dry-mode flow. Implication: the trigger label stays on the issue, so every subsequent fetcher sweep re-picks the same issue. That is fine in a test environment; never run a production cron with `DRY_RUN=true`.

What happens for each trigger state:

| Trigger state at sweep time | Default behaviour | `DRY_RUN=true` |
|---|---|---|
| **No `ymir_*` labels** (fresh issue) | Fetcher pushes to `triage_queue`. Agent stamps `ymir_triage_in_progress`, runs triage, writes a terminal `ymir_*` label. Result comment is suppressed unless the resolution is `not-affected` or `postponed`. If the run auto-chains to rebase or backport, the downstream agent moves the Jira workflow status to "In Progress" when it pops the task. | Agent runs triage but `set_jira_labels` / `add_jira_comment` short-circuit on `DRY_RUN`. No labels, no comment, no MR, no workflow status change. Issue untouched in Jira. |
| **`ymir_todo`** (assignee in `Red Hat Employee`, no `_in_progress`) | Fetcher atomically flips `ymir_todo` → `ymir_triage_in_progress`, pushes with `user_triggered=True`. Agent posts a private ack comment and a result comment on completion. `_failed` labels are written normally. Workflow status change is the same as the fresh-issue path (set by rebase/backport on auto-chain). | Fetcher skips the atomic flip (`ymir_todo` stays on the issue) but still pushes to Redis with `user_triggered=True`. Agent runs in dry mode and writes nothing; workflow status not changed. **Subsequent fetcher sweeps will re-push the same issue** because the trigger label was never consumed. |
| **`ymir_retry_needed`** (no `_in_progress`) | Fetcher atomically flips `ymir_retry_needed` → `ymir_triage_in_progress`, pushes with `user_triggered=False`. Agent runs full triage; behaves exactly like a fresh-issue run (no ack comment, result comment only for `not-affected`/`postponed`). Workflow status change is the same as the fresh-issue path. | Fetcher skips the atomic flip (`ymir_retry_needed` stays on the issue) but still pushes to Redis with `user_triggered=False`. Agent runs in dry mode and writes nothing; workflow status not changed. Subsequent fetcher sweeps will re-push the same issue. |
| **`ymir_todo`** or **`ymir_retry_needed`** **+** any `ymir_*_in_progress` label | Fetcher skips. Not enqueued. Workflow status not affected. | Fetcher skips. Not enqueued. Workflow status not affected. |
| **Any other terminal `ymir_*` label** (e.g. `ymir_triaged_rebase`, `ymir_rebased`, `ymir_triage_errored`) | Fetcher skips. Re-run by adding `ymir_todo` (recommended — produces an ack + result comment) or `ymir_retry_needed`. Workflow status not affected. | Fetcher skips. Workflow status not affected. |

The JQL filter for `ymir_todo` is gated on `assignee in membersOf("Red Hat Employee")`, so non-RH-employee assignees adding `ymir_todo` are silently ignored by the fetcher.

---

**Last Updated:** 2026-05-27
