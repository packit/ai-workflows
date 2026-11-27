# Rebase MR consolidation

Temporal discovers sibling issues, waits for their triage results, and groups
compatible rebases into a single handoff to the rebase worker. The
[Temporal package-stream coordination guide](../README-agents.md#coordinate-a-package-stream-in-temporal)
describes the current workflow and recovery procedure.

## Rebase worker handoff

The package-stream workflow lists the sibling issues in the rebase task's
`consolidated_issues` field. The worker uses that list for the shared merge
request, commit, and Jira updates. An empty list represents a single-issue
rebase. [Rebase worker](../ymir/agents/rebase_agent.py)
