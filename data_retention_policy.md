# Data Retention Policy

**Last Updated:** 2026-03-04

---

## Retention Periods

| Data Type | Retention | Status | Storage |
|-----------|-----------|--------|---------|
| **Git repository clones** | 7 days | ✅ Configured | `git-repos` volume |
| **Phoenix observability traces** | Infinite (default) | ⚠️ Not configured | PostgreSQL (`phoenix-db-data`, 20Gi) |
| **Redis task queues** (Jira interaction history) | Indefinite | ⚠️ Not configured | `valkey-data` (2Gi) |
| **Temporary build artifacts** | Agent execution only | ✅ Automatic | Within git clones |
| **MR comments/history** | N/A | Stored in GitLab.com | External |

---

## Implementation Details

### Git Repository Clones (7 Days) ✅

**Configured in:** `ymir/tools/privileged/utils.py`
```python
REPO_CLEANUP_DAYS = 7
```

- Automatic cleanup on every `clone_repository` call
- Deletes all stale working directories older than 7 days based on modification time
- Steps into container directories (`applicability/`, `merge_requests/`) and cleans their children individually
- Implemented in `clean_stale_repositories()` function

---

### Phoenix Observability Traces ⚠️

**Current:** No retention policy configured (infinite retention). Phoenix uses PostgreSQL
as its database backend (migrated from SQLite). Data is stored in the `phoenix-db-data`
PVC (20Gi).

**Recommendation:** Add 7-day retention to align with git cleanup policy

```yaml
# openshift/deployment-phoenix.yml
env:
  - name: PHOENIX_DEFAULT_RETENTION_POLICY_DAYS
    value: "14"
```

**Reference:** [Phoenix Data Retention Docs](https://arize.com/docs/phoenix/settings/data-retention.md)

---

### Redis Task Queues (Jira Interaction History) ⚠️

**Current:** No TTL or eviction policy configured (indefinite retention)

**Contains:** Jira issue keys, processing metadata, workflow state, and task results

**Queues:**
- Input: `triage_queue`, `rebase_queue_c9s`, `rebase_queue_c10s`, `backport_queue_c9s`, `backport_queue_c10s`, `clarification_needed_queue`
- Results: `error_list`, `open_ended_analysis_list`, `completed_rebase_list`, `completed_backport_list`
- Supervisor: `supervisor_work_queue` (sorted set with time-based scores for retry scheduling)

**Note:** The `supervisor_work_queue` uses time-based scores to schedule retry delays (15 minutes), but items are not automatically removed by TTL or eviction policy. Unprocessed items can persist indefinitely until successfully completed or manually removed.

**Recommendation:** Configure eviction policy and set TTL on completed task lists

**Reference:** [Redis Data Retention Best Practices](https://oneuptime.com/blog/post/2026-01-21-redis-data-retention-policies/view)

---

## Summary

**Configured Retention:**
- ✅ Git clones: 7 days automatic cleanup

**Missing Retention:**
- ⚠️ Phoenix traces: No policy (defaults to infinite, stored in PostgreSQL on 20Gi volume)
- ⚠️ Redis queues (Jira interaction history): No policy (defaults to indefinite, limited by 2Gi volume)

**Next Review:** 2027-03-04

---

**Sources:**
- [Arize Phoenix Data Retention](https://arize.com/docs/phoenix/settings/data-retention.md)
- [Redis Data Retention Policies](https://oneuptime.com/blog/post/2026-01-21-redis-data-retention-policies/view)
- [Valkey Configuration Best Practices](https://www.percona.com/blog/valkey-redis-configuration-best-practices/)
