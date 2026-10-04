# MR Verification Agent

## Problem statement

Every Ymir-authored merge request is reviewed by a human RHEL maintainer before
it can be merged. That review is where the agents' mistakes are caught — a patch
with no upstream provenance, a `Release:` that was not bumped, a changelog entry
that references the wrong Jira issue, a stray `.orig` file. Those mistakes are
mechanical and repetitive, and they consume the scarcest resource in the loop:
maintainer attention.

The MR verification agent performs that mechanical pass first. It reads the MR,
checks it against a fixed list of RHEL packaging invariants, and posts a review
comment. It does not replace the human reviewer and it does not merge anything.
Its value is that a maintainer opening a Ymir MR already knows where to look.

## Design constraints

1. **The reviewer must not be able to modify what it reviews.** It gets a
   read-only tool set — no `create`/`str_replace`/`remove`, no
   `git_patch_apply`, no push tool, no Jira status tool. A reviewer that can
   "just fix it" would race the agent that owns the MR branch and would destroy
   the audit trail of what the producing agent actually generated.
2. **A broken reviewer must never block a good MR.** Every failure path
   degrades to `inconclusive` plus a Jira label. Submitting the review job is
   best-effort and wrapped in a `try`: it runs *after* the MR is already open,
   so nothing it does can fail the producing agent's run.
3. **Advisory by default.** The review is a plain MR comment. A package can opt
   into merge-blocking discussions, but it must opt in explicitly.

## Architecture

```
  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
  │   Backport   │  │    Rebase    │  │   Rebuild    │  │  MR Consolidation│
  │    Agent     │  │    Agent     │  │    Agent     │  │      Agent       │
  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘
         │  MR opened      │                 │                   │
         └─────────────────┴────────┬────────┴───────────────────┘
                                    │
                   tasks.try_submit_verification_job()
                                    │  (checks MR_VERIFICATION_ENABLED
                                    │   and ymir.yaml `verification`)
                                    ▼
              ┌───────────────────────────────────────────┐
              │ Redis list queues                         │
              │  mr_verification_queue_c9s   (+ _todo)    │
              │  mr_verification_queue_c10s  (+ _todo)    │
              └───────────────────┬───────────────────────┘
                                  │ BRPOP [todo, normal]
                     ┌────────────┴────────────┐
                     ▼                         ▼
        ┌───────────────────────┐  ┌───────────────────────┐
        │ mr-verification-agent │  │ mr-verification-agent │
        │        -c9s           │  │        -c10s          │
        └───────────┬───────────┘  └───────────┬───────────┘
                    └─────────────┬────────────┘
                                  │ issue_lock("lock:mr-verify:<url>")
                                  ▼
                    ┌─────────────────────────────┐
                    │ MRVerificationWorkflow      │
                    │  1. prepare_clone           │
                    │  2. collect_evidence        │
                    │  3. run_verification_agent  │
                    │  4. publish_verdict         │
                    │  5. record_result           │
                    └──────────┬──────────────────┘
                               │
             ┌─────────────────┼──────────────────┐
             ▼                 ▼                  ▼
      MR comment +     Jira comment       completed_mr_verification_list
      MR label         (blockers only)    + reviewed-head hash
```

## Queue design

Ordinary Redis **list** queues, consumed with `BRPOP`, exactly like the backport
and rebuild queues — not the Lua hash queue used by MR consolidation. The hash
queue exists to enforce at-most-one-active job per *package/branch* pair;
verification has no such invariant. Two MRs for the same package can be reviewed
concurrently without interfering, because each review only reads.

What must not happen is two workers reviewing the *same* MR and double-posting,
so `process_task` takes an `issue_lock` keyed on the MR URL
(`lock:mr-verify:<url>`) rather than on the Jira issue.

| Queue | Purpose |
|-------|---------|
| `mr_verification_queue_c9s` / `_c10s` | Review jobs, split by container so `rpmbuild -bp` runs against the right build root |
| `mr_verification_queue_c9s_todo` / `_c10s_todo` | Priority twins for `ymir_todo`-triggered runs |
| `completed_mr_verification_list` | `MRVerificationOutputSchema` records for inspection |
| `mr_verification_reviewed_head:<MR URL>` | Head SHA of the last review, used to skip re-reviewing an unchanged MR. Expires after `MR_VERIFICATION_REVIEWED_TTL` (90 days) so it does not accumulate — the namespace has no eviction policy |

The verification queues are in `RedisQueues.input_queues()` but are deliberately
**excluded from the Jira issue fetcher's dedup scan**. A review is advisory work
that happens after the MR already exists; a queued — or stuck — review must not
stop a maintainer from re-triggering the issue with `ymir_todo`.

## Workflow steps

1. **`prepare_clone`** — clone the MR's source branch via
   `prepare_dist_git_from_merge_request`, read the MR title/description/target
   branch, resolve the Jira key (from the task, or parsed out of the MR
   description), and record `git rev-parse HEAD`. If that SHA matches the one in
   `mr_verification_reviewed_heads`, the MR has not changed since the last
   review and the workflow ends (the marker is only written by non-dry-run
   reviews, so `DRY_RUN=true` never suppresses a real one). Then load the
   package's `verification` config
   and bail out if the package opted out.
2. **`collect_evidence`** — fetch the target branch into the clone (via the
   MCP gateway's `fetch_branch`, followed by the usual 60 s NFS
   attribute-cache wait), compute `git diff <target>...HEAD` and the changed
   file list, truncate the diff to `MR_VERIFICATION_MAX_DIFF_CHARS`, and pull
   the MR's failed pipeline jobs. A missing or still-running pipeline is normal
   and is not an error.
3. **`run_verification_agent`** — run the LLM over the read-only tool set with
   a source-agent-specific checklist (a rebase is reviewed differently from a
   one-patch backport), producing an `MRVerificationOutputSchema`.
4. **`publish_verdict`** — post the rendered review as an MR comment (a
   *blocking* discussion only when the package set `block_on_findings: true`
   **and** the review found a blocker), label the MR `ymir_mr_verified` or
   `ymir_mr_changes_requested`, and, when changes were requested, add a Jira
   comment so the issue's watchers see it.
5. **`record_result`** — store the reviewed head SHA and push the structured
   result onto `completed_mr_verification_list`.

## Verdicts, severities and labels

| Verdict | Meaning | MR label |
|---------|---------|----------|
| `approved` | No blockers. Warnings and nitpicks may still be reported. | `ymir_mr_verified` |
| `changes_requested` | At least one blocker. | `ymir_mr_changes_requested` |
| `inconclusive` | Not enough evidence to judge (sources unavailable, tooling failure, agent crash). | none |

`inconclusive` is deliberately unlabelled: an absent verdict is honest, and
labelling it would let a reviewer mistake "we could not check" for "we checked".
An agent crash that survives all retries sets the Jira label
`ymir_mr_verification_errored` and never touches the MR.

Finding severities: `blocker` (do not merge as-is), `warning` (worth a second
look), `nitpick` (style). Only `blocker` changes the verdict.

## Per-package configuration

In `gitlab.com/redhat/centos-stream/rules/<package>/ymir.yaml`:

```yaml
verification:
  # Review Ymir-authored MRs for this package. Default: true.
  verify_mrs: true
  # Post blocker findings as an unresolved (merge-blocking) discussion instead
  # of a plain comment. Default: false — opt in only.
  block_on_findings: false
```

Unlike `consolidation` and `reproducer`, verification defaults to **enabled**:
reviewing an MR only ever posts a comment, so it is safe to opt out of rather
than into. A missing or malformed `verification` section falls back to the
defaults and is logged; it never stops the review.

## Global configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `MR_VERIFICATION_ENABLED` | `true` | Kill switch. When false, producing agents stop enqueueing; already-queued jobs still drain. |
| `MR_VERIFICATION_MAX_DIFF_CHARS` | `120000` | How much of the diff is pasted into the prompt. Beyond this the agent reads the clone directly. |
| `MR_VERIFICATION_REVIEWED_TTL` | `7776000` (90 d) | Lifetime of the already-reviewed marker. |
| `CHAT_MODEL_MR_VERIFICATION` | `""` | Per-agent model override. |
| `CONTAINER_VERSION` | `c10s` | Selects which queue pair the pod consumes. |

## Running the agent

Against a single MR, without touching GitLab or Jira:

```bash
make run-mr-verification-agent-standalone \
    MERGE_REQUEST_URL=https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/42 \
    JIRA_ISSUE=RHEL-12345 \
    SOURCE_AGENT=Backport \
    DRY_RUN=true
```

In the full pipeline the agent runs under the `agents` compose profile as
`mr-verification-agent-c9s` / `-c10s`; follow it with `make logs-mr-verification`.

On OpenShift: `deployment-mr-verification-agent-c9s.yml` and `-c10s.yml`,
`make -C openshift logs-mr-verification-c10s`,
`make -C openshift show-mr-verification-queue-c10s`.

## Safety invariants

| Invariant | How it is enforced |
|-----------|--------------------|
| The reviewer never modifies the MR branch | Read-only tool set; no push/edit/patch-apply tools are passed to the agent |
| The reviewer never blocks a merge unless asked | Plain comment unless `block_on_findings: true` *and* a blocker was found |
| A failed review never fails the producing agent | `try_submit_verification_job` catches everything and logs |
| A failed review never blocks the MR | Crashes produce a Jira label only; no MR label, no MR comment |
| The same MR is never reviewed twice concurrently | `issue_lock` on the MR URL |
| An unchanged MR is never re-reviewed | Reviewed head SHA recorded in `mr_verification_reviewed_heads` |
| A pending review never blocks re-triage of the issue | Verification queues are skipped by the fetcher's dedup scan |
| In-flight reviews survive a rollout | `run_task_loop(shutdown_event=…)` re-pushes to Redis on SIGTERM; `terminationGracePeriodSeconds: 45` |

## File map

| Concern | File |
|---------|------|
| Agent and workflow | `ymir/agents/mr_verification_agent.py` |
| Prompts | `ymir/agents/prompts/mr_verification/{instructions,prompt}.j2` |
| Job submission, config loading | `ymir/agents/tasks.py` (`try_submit_verification_job`, `fetch_verification_config`) |
| Queues, labels | `ymir/common/constants.py` |
| Schemas | `ymir/common/models.py` (`MRVerification*`, `PackageVerificationConfig`) |
| Compose services | `compose.yaml` (`mr-verification-agent-c9s`, `-c10s`) |
| OpenShift | `openshift/deployment-mr-verification-agent-{c9s,c10s}.yml` |
| Unit tests | `ymir/agents/tests/unit/test_mr_verification_agent.py` |
