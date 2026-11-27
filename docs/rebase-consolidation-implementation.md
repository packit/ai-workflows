# Rebase MR consolidation

Temporal discovers sibling issues, waits for their triage results, and groups
compatible rebases into a single handoff to the rebase worker. The
[Temporal package-stream coordination guide](../README-agents.md#coordinate-a-package-stream-in-temporal)
describes the current workflow and recovery procedure.

## Legacy Jira fallback

When a rebase task has no `consolidated_issues`, the rebase worker searches
the same package and fix-version stream for issues labeled
`ymir_triaged_rebase`. It includes an issue when its Jira comments contain the
legacy “Queued for triage as potential sibling of” marker naming that primary.
A rebase task whose own issue has a sibling marker exits before opening a
separate MR. See the [rebase worker](../ymir/agents/rebase_agent.py)
and [Jira sibling lookup](../ymir/agents/rebase_consolidation.py).
