# Jira Label-Based Workflow Routing

This document describes how Jira labels control workflow routing through the processing pipeline.

## Label State Machine

```mermaid
stateDiagram-v2
    [*] --> New: Issue Created
    New --> InProgress: jotnar_*_in_progress
    InProgress --> Success: jotnar_rebased/backported
    InProgress --> Failed: jotnar_*_failed
    InProgress --> Errored: jotnar_*_errored
    Success --> Merged: jotnar_merged
    Failed --> Retry: jotnar_retry_needed
    Errored --> Attention: jotnar_needs_attention
    Retry --> InProgress: Retry Processing
    New --> Triaged: jotnar_triaged
    Merged --> [*]
    Triaged --> [*]
```

## Redis Queue Routing

```mermaid
flowchart TD
    FETCH[Jira Issue Fetcher]

    FETCH -->|No jötnar labels<br/>OR jotnar_retry_needed| TRIAGE[triage_queue]

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
| `jotnar_rebase_in_progress` | Rebase starts | Rebase completes/fails | `jotnar_rebased` or `jotnar_rebase_failed` |
| `jotnar_backport_in_progress` | Backport starts | Backport completes/fails | `jotnar_backported` or `jotnar_backport_failed` |
| `jotnar_rebased` | Rebase success | Never | `jotnar_merged` |
| `jotnar_backported` | Backport success | Never | `jotnar_merged` |
| `jotnar_merged` | MR merged | Never | Final state |

### Error Labels

| Label | Meaning | Blocks Retry? | Action |
|-------|---------|---------------|--------|
| `jotnar_needs_attention` | Human intervention needed | ✅ Yes | Fix issue, remove label, add `jotnar_retry_needed` |
| `jotnar_triage_errored` | Triage failed | ✅ Yes | Check error_list |
| `jotnar_rebase_errored` | Rebase error | ✅ Yes | Check Jira comment |
| `jotnar_backport_errored` | Backport error | ✅ Yes | Check Jira comment |
| `jotnar_rebase_failed` | Rebase unsuccessful | ❌ No | May auto-retry |
| `jotnar_backport_failed` | Backport unsuccessful | ❌ No | May auto-retry |

### Control Labels

| Label | Purpose | Effect |
|-------|---------|--------|
| `jotnar_retry_needed` | Trigger retry | Forces reprocessing |
| `jotnar_triaged` | Triage completed, no automated follow-up | Terminal state |
| `jotnar_fusa` | Functional Safety | Requires maintainer review |

## Queue Types Summary

| Queue | Type | Triggers | Labels Added | Status |
|-------|------|----------|--------------|--------|
| `triage_queue` | Input | No labels OR retry_needed | - | Active |
| `rebase_queue_c9s` | Input | Resolution=REBASE, RHEL 8/9 | `jotnar_rebase_in_progress` | Active |
| `rebase_queue_c10s` | Input | Resolution=REBASE, RHEL 10+ | `jotnar_rebase_in_progress` | Active |
| `backport_queue_c9s` | Input | Resolution=BACKPORT, RHEL 8/9 | `jotnar_backport_in_progress` | Active |
| `backport_queue_c10s` | Input | Resolution=BACKPORT, RHEL 10+ | `jotnar_backport_in_progress` | Active |
| `rebase_queue` | Input | (Not actively enqueued) | `jotnar_rebase_in_progress` | Legacy (checked for deduplication) |
| `backport_queue` | Input | (Not actively enqueued) | `jotnar_backport_in_progress` | Legacy (checked for deduplication) |
| `clarification_needed_queue` | Input | Resolution=CLARIFICATION | `jotnar_needs_attention` | Active |
| `error_list` | Output | Any error | `jotnar_*_errored` | Active |
| `open_ended_analysis_list` | Output | Resolution=OPEN_ENDED_ANALYSIS | `jotnar_triaged` | Active |
| `completed_rebase_list` | Output | Rebase success | `jotnar_rebased` | Active |
| `completed_backport_list` | Output | Backport success | `jotnar_backported` | Active |

## Deduplication Logic

**Note:** The Jira Issue Fetcher only decides whether to queue an issue for processing based on labels. The actual label cleanup (including removal of `jotnar_retry_needed`) happens in the Triage Agent after it consumes the task from the queue.

```mermaid
flowchart TD
    START[Jira Issue Fetcher<br/>Found issue]
    CHECK{Has any<br/>jötnar_* label?}
    RETRY{Has<br/>jotnar_retry_needed?}

    START --> CHECK
    CHECK -->|No| ADD[Add to triage_queue]
    CHECK -->|Yes| RETRY
    RETRY -->|Yes| ADD
    RETRY -->|No| SKIP[Skip - already processed]

    ADD --> TRIAGE_PROCESS[Triage Agent processes issue]
    TRIAGE_PROCESS --> CLEANUP[Triage Agent removes<br/>all jötnar_* labels]

    style ADD fill:#c8e6c9
    style SKIP fill:#ffcdd2
    style CLEANUP fill:#e1f5fe
```

---

**Last Updated:** 2026-03-03
