# Threat Model: Ymir (ai-workflows) — AI-Automated RHEL/CentOS Stream Packaging

## 1. System context

Ymir (internally also referred to by its former name "Jötnar" in some
identifiers — service accounts, Kerberos principals, the `quay.io/jotnar`
registry namespace) is a set of LLM-driven agents, built on the BeeAI
framework, that automate RHEL/CentOS Stream package maintenance. It triages
Jira issues, opens/updates
GitLab merge requests for rebases, backports, rebuilds, and MR
consolidation against internal dist-git and `gitlab.com/redhat` repos,
monitors CI/build/test results, and advances issues through errata and
release via Jira status/field changes. A separate "Supervisor"
(collector/processor) subsystem drives the testing/release phase using a
Redis/Valkey work queue. The system runs as a fleet of OpenShift
Deployments and CronJobs in the `jotnar-ymir` namespace on Red Hat's
internal GPC cluster, operated by the Ymir team (bot identity
`redhat-ymir-agent`).

Security-relevant assumptions this system makes about its environment:

- **The OpenShift `TenantEgress` default-deny network policy is the
  primary boundary** preventing arbitrary outbound connections from agent
  pods. Several tool capabilities (e.g. fetching an attacker-influenced
  URL) are SSRF-shaped and are mitigated at the network layer, not by
  in-application URL validation.
- **Agents never trigger RHEL/CentOS Stream builds directly.** Build
  triggering (Brew/Konflux) is delegated to GitLab CI via a
  human-applied MR label (`feature::draft-builds::enabled`); agents only
  manipulate MR labels and metadata, never call build-trigger APIs.
- **All AI-authored changes require human review before merge** — the
  `ymir_needs_attention` label is applied to every new MR, and this is
  treated as a hard control, not a suggestion.
- **Automated processing can only be maintainer-triggered.** Adding the
  `ymir_todo` label only enqueues work if the fetcher verifies (via a Jira
  changelog walk, not JQL) that the label-adder is a member of the
  "Red Hat Employee" Jira group.
- **Tool credentials are partitioned into "privileged" and "unprivileged"
  sets** (`ymir/tools/privileged/` vs `ymir/tools/unprivileged/`), enforced
  by a pre-push static check. This separates *which tools exist with
  secrets*, but the LLM agent itself has runtime access to call both sets
  — it does not sandbox the model away from privileged tools.
- **`DRY_RUN` and `JIRA_ALLOW_STATUS_CHANGES` are safety gates** that must
  default to safe values (`false`) in any new deployment; flipping them is
  an explicit, auditable operational decision.

## 2. Assets

| asset | description | sensitivity |
|---|---|---|
| Dist-git / GitLab write access (bot identity `redhat-ymir-agent`) | Ability to fork, push, and open MRs against internal and `gitlab.com/redhat` RHEL & CentOS Stream package repos | critical |
| Kerberos keytabs (personal + `redhat-ymir-agent.keytab` prod) | Enable passwordless authentication and user impersonation against internal Red Hat systems (Brew/Koji, dist-git SSH) | critical |
| GitLab PAT (`redhat-ymir-agent.gitlab.token`) | OAuth2 token with write access to `gitlab.com` dist-git repos and MRs | critical |
| Source, patches, and spec-file changes produced by agents | Content that flows directly into RHEL/CentOS Stream builds if merged | critical |
| Local `.secrets/` credential bundle (dev machines, CI runners) | Co-located keytabs, GitLab PAT, Jira token, GCP keys | critical |
| Jira API token | Read/write access to RHEL/PACKIT Jira issues, including status transitions that trigger errata creation and compose inclusion | high |
| GCP Vertex AI service-account keys (dev/prod) | Access to hosted LLM inference; misuse enables cost abuse or model manipulation | high |
| Redis/Valkey task queues (`triage_queue`, `supervisor_work_queue`, `merge_consolidation_queue`) | Integrity of in-flight automation state; controls which package/branch/issue is processed next | high |
| Container images (`quay.io/jotnar/*`) | Build supply-chain integrity for all deployed agent/service images | high |
| Testing Farm API token | Ability to trigger test runs against package builds | medium |
| Splunk logs / OTEL traces (Phoenix) | May contain tool inputs/outputs; sensitive if credential redaction fails | medium |

## 3. Entry points & trust boundaries

| entry_point | description | trust_boundary | reachable_assets |
|---|---|---|---|
| Jira issue content (title/description/comments) | Ingested by `jira-issue-fetcher` cronjobs and the triage agent via JQL search | untrusted Jira reporter content → LLM agent context / privileged tool arguments | Dist-git write access, GitLab PAT, Kerberos keytabs, source/patch integrity |
| `ymir_todo` Jira label | Maintainer-facing trigger to enqueue processing of a specific issue | Jira user → triage queue enqueue (gated by Red Hat-employee group check) | Redis/Valkey task queues, Jira API token |
| GitLab MR comments / CI pipeline logs | Read by agents during iterate/fix loops on open MRs | untrusted GitLab.com commenter / CI log content → LLM agent context | Dist-git write access, GitLab PAT |
| `GetPatchFromUrlTool` `patch_url` parameter | Tool argument that can originate from Jira issue text | attacker-influenceable input → outbound HTTP fetch (SSRF-shaped) | Internal network reachability within egress allow-list |
| `redis-commander` OpenShift Route | Public HTTPS route to the Valkey queue admin UI, no application-level auth configured | remote unauth peer → Redis/Valkey read/write | Redis/Valkey task queues |
| `phoenix` OpenShift Route | Public HTTPS route to the tracing/observability UI | remote peer → trace data | Splunk logs / OTEL traces |
| `trace-server` OpenShift Route | Public HTTPS OTEL ingestion/query endpoint | remote peer → trace data | Splunk logs / OTEL traces |
| `oc exec` / `oc rsh` into the `valkey` pod | Documented operational practice for queue inspection (`redis-cli KEYS/LPUSH/LRANGE`) | OpenShift-RBAC-authorized operator → direct queue read/write, bypassing application logic | Redis/Valkey task queues |
| GitLab CI build pipeline (`gitlab.cee.redhat.com`) → `quay.io/jotnar` | CI builds and pushes all agent/service container images using a robot account | internal CI service account → container registry write | Container images |
| PyPI dependency resolution during container build | `pip`/`uv` install of Python packages (e.g. `litellm`) into every agent image | third-party package maintainer → code executing with full pod privileges | Container images, all credentials mounted into that pod |

## 4. Threats

| id | threat | actor | surface | asset | impact | likelihood | status | controls | evidence |
|---|---|---|---|---|---|---|---|---|---|
| T1 | Unauthenticated public route grants direct read/write access to live automation task queues | remote_unauth | `redis-commander` OpenShift Route | Redis/Valkey task queues | medium | unlikely | unmitigated | no `HTTP_USER`/`HTTP_PASSWORD` configured; only Red Hat employees can tamper with it | none |
| T2 | Indirect prompt injection via untrusted Jira issue or GitLab MR comment content causes an agent to misuse privileged tools (unauthorized push, credential exfiltration, SSRF via `patch_url`) | remote_auth | Jira issue content; GitLab MR comments; `GetPatchFromUrlTool` | Dist-git write access, GitLab PAT, Kerberos keytabs, source/patch integrity | critical | possible | partially_mitigated | credential redaction (`_REDACT_PATTERNS` in `gateway.py`); privileged/unprivileged tool split (LLM can still call both); network egress allow-list bounds SSRF blast radius; mandatory human review before merge | commit `c5db93b3` (credential leakage), commit `8b181341`/`b294407e` (path traversal) |
| T3 | Supply-chain compromise of a third-party Python dependency executes arbitrary code inside agent/build containers, exposing all mounted credentials | supply_chain | PyPI dependency resolution during container build | Container images, Kerberos keytabs, GitLab PAT, GCP Vertex AI keys | critical | possible | mitigated | version pins excluding known-compromised `litellm` releases; build-time `litellm_init.pth` malicious-file detection; enforced Log Detective MCP version | commit `e7422175` and related `litellm` version pins (real PyPI supply-chain compromise of `litellm` 1.82.7/1.82.8) |
| T4 | Credential material leaks into LLM agent context or centralized logs via tool error/stderr output | insider | privileged GitLab/dist-git tool error handling; Splunk-forwarded stdout/stderr | GitLab PAT, Kerberos keytabs | critical | rare | mitigated | `_sanitize_git_stderr()` and `_REDACT_PATTERNS` strip credential-shaped strings before they reach agent context or logs | commit `c5db93b3` |
| T5 | Operator (or anyone with `oc exec`/`oc rsh` RBAC into the `valkey` pod) directly injects or tampers with queue entries, controlling which package/branch/issue privileged agents act on | local_admin | `oc exec`/`oc rsh` into `valkey` pod | Redis/Valkey task queues | high | possible | partially_mitigated | OpenShift namespace RBAC restricts who can `oc exec`; no application-level audit trail for direct queue mutation | `investigating-issues.md` documents this as routine operational practice |
| T6 | SSRF-shaped fetch of an attacker-supplied `patch_url` reaches internal network endpoints reachable from the agent pod | remote_auth | `GetPatchFromUrlTool` `patch_url` parameter | Internal network reachability, GCP Vertex AI endpoints, other RH internal services within the egress allow-list | high | possible | partially_mitigated | OpenShift `TenantEgress` default-deny egress allow-list (network-level only; no in-app URL validation) | none |
| T7 | Attacker-influenced or malformed `jira_issue` string reaches a privileged filesystem operation (`shutil.rmtree`) without validation, deleting arbitrary directories on the shared clone volume | remote_auth | `clone_and_prep_sources`, `fork_and_prepare_dist_git` | Shared PVC `mcp-server-git-repos` (RWX, mounted by 6+ agent pods) | high | rare | mitigated | input validation rejects empty, absolute, or `..`-containing `jira_issue` values | commit `8b181341`, `b294407e`, `3dad71ea`/`39116842` |
| T8 | Non-employee or compromised Jira account forces automated processing of an arbitrary issue via the `ymir_todo` label | remote_auth | `ymir_todo` Jira label | Dist-git write access, Jira write access, compute/API budget | high | rare | mitigated | fetcher verifies label-adder is a Red Hat Employee Jira-group member via changelog walk (not JQL); atomic label flip before enqueue (fail-closed) | none |
| T9 | Unbounded retention of Redis/Valkey queue data and Phoenix traces stores workflow state and tool inputs/outputs indefinitely | insider | Valkey queue storage; Phoenix trace PVC | Redis/Valkey task queues, Splunk logs / OTEL traces | medium | likely | unmitigated | none — `data_retention_policy.md` explicitly flags this as unresolved; the 7-day cleanup job only covers git clones, not queues or traces | `data_retention_policy.md` self-identifies the gap |
| T10 | Runtime users in the highest-privilege images (`beeai`, `mcp`, `supervisor`) are members of the `wheel` group; if the base image ships a sudoers rule, a compromised process (via T2/T3) could escalate to root inside the container | local_user | `Containerfile.c9s`/`.c10s`, `Containerfile.mcp`, `Containerfile.supervisor` | Pod filesystem/process, mounted credentials | medium | rare | unmitigated | `runAsNonRoot: true` and `seccompProfile: RuntimeDefault` enforced by OpenShift SCC; sudoers presence on the base image has not been verified | none |
| T11 | Long-running Deployments (agents, mcp-gateway, supervisor-processor, phoenix) do not set `allowPrivilegeEscalation: false` or `capabilities: drop: [ALL]`, unlike CronJob pods which do | local_user | `openshift/deployment-*.yml` | Pod isolation guarantees | medium | rare | unmitigated | `runAsNonRoot` + `seccompProfile` only | none |

Sorted by (impact desc, likelihood desc).

## 5. Deprioritized

| threat | reason |
|---|---|
| Direct compromise of Brew/Konflux build triggering | Agents never call build-trigger APIs directly; the actual trigger is GitLab CI acting on a human-applied MR label. Threat belongs to the CI/build-system's own threat model, not this repo's. |
| Processing of embargoed CVEs by agents | Explicitly out of scope by policy — agents do not handle embargoed issues (`monitoring.md`). Enforced procedurally, not by this codebase. |
| Compromise of GitLab CI build infrastructure (`gitlab.cee.redhat.com`) or the `quay.io` robot account | Owned and secured by central Red Hat CI/PSI infrastructure teams, outside this project's control. |
| OpenShift platform/cluster-level compromise (node escape, cluster-admin compromise, storage-class or admission-webhook bugs) | Platform-team responsibility; this project treats the OpenShift control plane as trusted infrastructure. |
| Self-inflicted availability incidents (RollingUpdate/PVC deadlock, resource-quota deadlock, stale AWS `nodeSelector`s from cluster migration) | Already fixed (all Deployments use `Recreate` strategy); these were operational reliability bugs, not adversarial threats — tracked as ops incidents. |

## 6. Open questions

- Does the base image (`quay.io/centos/centos:stream9/10`, `fedora:43`/`44`) ship a sudoers rule that grants `wheel`-group members any form of `sudo`? Needs verification — affects T10.
- Is the `redis-commander` route's lack of authentication an intentional decision (relying on hostname secrecy / network policy) or an oversight? This is currently the single highest-severity unmitigated item (T1) and needs an explicit maintainer decision.
- Who can file Jira issues or post GitLab MR comments that reach agent context — can non-Red-Hat-employee partners or customers do so? This determines whether T2's actor should be `remote_unauth` rather than `remote_auth`.
- `ai_providers_data_flow.md` describes "Gemini" as the primary model, but `configmap-chat-env.yml` shows `vertexai:claude-opus-4-6` — the doc is stale and should be corrected (may indicate other stale assumptions elsewhere).
- Phoenix trace retention is described inconsistently: `monitoring.md` says 2 weeks, `data_retention_policy.md` says unconfigured/infinite, and PVC size differs between docs (10Gi) and the actual manifest (150Gi). Needs reconciliation and an enforced retention job.
- Is `SKIP_GATEWAY_CHECK=1` (the bypass for `check-unprivileged-gateway.py`'s privileged/unprivileged tool separation check) ever used in CI or production, and if so, is its use audited?
- Does the external `access_control.md` (referenced from `SECURITY.md`, not present in this repo) cover credential rotation for the long-lived keytabs, GitLab PAT, and GCP keys? Should be cross-referenced from this document once confirmed.
- No maintainer interview has been conducted for this document yet — the impact/likelihood scores above are a bootstrap author's best estimate from code and docs, not a maintainer-agreed baseline.

## 7. Provenance

```markdown
- mode: bootstrap
- date: 2026-07-30
- target: github.com/packit/ai-workflows @ dc10a6c1
- inputs: SECURITY.md, README.md, README-agents.md, README-supervisor.md, AGENTS.md,
  CONTRIBUTING.md, ai_providers_data_flow.md, brew_konflux_data_flow.md,
  gitlab_distgit_data_flow.md, jira_data_flow.md, jira_label_workflow_routing.md,
  docs/mr_consolidation_architecture.md, docs/network-egress-compliance.md,
  monitoring.md, data_retention_policy.md, deployment_adventures.md,
  deployment_adventures_2.md, investigating-issues.md, openshift/ manifests,
  ymir/tools/privileged and ymir/tools/unprivileged source, git commit history
- owner: unset
```

## 8. Recommended mitigations

| mitigation | threat_ids | closes_class | effort |
|---|---|---|---|
| Require authentication (proxy or `HTTP_USER`/`HTTP_PASSWORD`) on the `redis-commander` route, or remove the public Route and access it only via `oc port-forward` | T1 | yes | S |
| Add TTL/eviction for Redis/Valkey queue entries and an enforced Phoenix trace retention job; reconcile the retention numbers across `monitoring.md`, `data_retention_policy.md`, and the PVC manifests | T9 | yes | M |
| Add explicit destination validation/allow-listing for `GetPatchFromUrlTool`'s `patch_url` (beyond relying solely on network egress policy) as defense-in-depth against SSRF | T6 | partial | M |
| Restrict OpenShift RBAC so `oc exec`/`oc rsh` into `valkey` (and other stateful pods) is limited to a small break-glass group, and add audit logging for direct queue mutation | T5 | partial | M |
| Treat all Jira-issue- and MR-comment-derived text as untrusted at the prompt level; add a confirmation/second-check step before high-impact privileged actions (push, Jira status change) whose triggering content originated from unverified external sources | T2 | partial | L |
