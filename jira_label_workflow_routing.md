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

    FETCH -->|No Ymir labels<br/>OR ymir_retry_needed| TRIAGE[triage_queue]

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

## Queue Types Summary

| Queue | Type | Triggers | Labels Added | Status |
|-------|------|----------|--------------|--------|
| `triage_queue` | Input | No labels OR retry_needed | - | Active |
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

**Note:** The Jira Issue Fetcher only decides whether to queue an issue for processing based on labels. The actual label cleanup (including removal of `ymir_retry_needed`) happens in the Triage Agent after it consumes the task from the queue.

```mermaid
flowchart TD
    START[Jira Issue Fetcher<br/>Found issue]
    CHECK{Has any<br/>ymir_* label?}
    RETRY{Has<br/>ymir_retry_needed?}

    START --> CHECK
    CHECK -->|No| ADD[Add to triage_queue]
    CHECK -->|Yes| RETRY
    RETRY -->|Yes| ADD
    RETRY -->|No| SKIP[Skip - already processed]

    ADD --> TRIAGE_PROCESS[Triage Agent processes issue]
    TRIAGE_PROCESS --> CLEANUP[Triage Agent removes<br/>all ymir_* labels]

    style ADD fill:#c8e6c9
    style SKIP fill:#ffcdd2
    style CLEANUP fill:#e1f5fe
```

---

**Last Updated:** 2026-03-03
