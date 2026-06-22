# OpenShift Deployment

## Deployment Location

Agents are deployed in the `jotnar-ymir--jotnar-ymir` project.
- **Console:** https://console-openshift-console.apps.gpc.ocp-hub.prod.psi.redhat.com/k8s/cluster/projects/jotnar-ymir--jotnar-ymir
- **Project:** jotnar-ymir--jotnar-ymir

## Steps to deploy:

- Ensure secrets exist for the following values:

  `gitlab-env`:
  ```
  GITLAB_TOKEN
  ```

  `jira-env`:
  ```
  JIRA_TOKEN
  ```

  `redhat-ymir-agent-keytab`:
  ```
  oc create secret generic redhat-ymir-agent-keytab --from-file=redhat-ymir-agent.keytab
  ```

  `testing-farm-env`:
  ```
  TESTING_FARM_API_TOKEN
  ```

  `sentry-env`:
  ```
  SENTRY_DSN
  ```

  Values of these secrets are documented in [README](https://github.com/packit/jotnar?tab=readme-ov-file#service-accounts--authentication).

- Create RHEL configuration ConfigMap manually:

  ```bash
  # Get rhel-config.json from Bitwarden (contains info about RHEL versions)
  # Then create ConfigMap:
  oc create configmap rhel-config --from-file=rhel-config.json
  ```

  The `rhel-config.json` file is stored in [jotnar](https://github.com/packit/jotnar) repo.

- Create Vertex AI secret:

  ```bash
  oc create secret generic vertex-key --from-file=jotnar-vertex-prod.json
  ```
  You can obtain the file from our bitwarden.

- Verify the storage class works on the cluster before deploying. The default storage class shown by
  `oc get storageclass` may be blocked by an admission webhook that isn't visible in its description.
  Test with a throwaway PVC first:

  ```bash
  oc apply -f - <<EOF
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: test-pvc
  spec:
    storageClassName: netapp-nfs
    accessModes: [ReadWriteOnce]
    resources:
      requests:
        storage: 1Mi
  EOF
  oc delete pvc test-pvc
  ```

  If the webhook rejects it, try a different storage class.

- Run the deployment script:

  ```bash
  ./openshift/deploy.sh
  ```

  This applies all configurations: egress rules, ConfigMaps, ImageStreams, PersistentVolumes, Services, and Deployments.

## Jira Issue Fetcher Deployment

Two CronJobs run the fetcher with different JQL queries:

| CronJob | Schedule | QUERY | ConfigMap |
|---|---|---|---|
| `jira-issue-fetcher` | `*/30 * * * *` | A generic filter for processing a batch of issues (e.g. early adopters) — currently `filter = "Ymir early adopters CVEs"` | `jira-issue-fetcher-filter-env` |
| `jira-issue-fetcher-todo` | `*/5 * * * *` | `labels = "ymir_todo"` | `jira-issue-fetcher-todo-env` |

Both share the common knobs (`IGNORED_COMPONENTS`, `MAX_ISSUES`, `LOGLEVEL`) from `jira-issue-fetcher-env`. Each pod mounts the shared configmap plus its per-cron QUERY configmap. To target a different batch, edit `configmap-jira-issue-fetcher-filter-env.yml` and re-apply.

Manually run either fetcher:

```bash
make run-jira-issue-fetcher       # generic batch filter
make run-jira-issue-fetcher-todo  # ymir_todo sweep
```

`jira-issue-fetcher-todo` ships with `suspend: false` (it runs on its schedule out of the box); `jira-issue-fetcher` ships with `suspend: true` and must be resumed before it fires. Enable or pause each one's schedule:

```bash
make unsuspend-jira-issue-fetcher        # resume the generic batch fetcher
make unsuspend-jira-issue-fetcher-todo   # resume the ymir_todo sweep
make suspend-jira-issue-fetcher          # pause it again
make suspend-jira-issue-fetcher-todo     # pause it again
```

These patch the live CronJob (`oc patch ... suspend`). Re-applying the manifests (`./deploy.sh`) resets each CronJob to whatever `suspend` value its `cronjob-jira-issue-fetcher*.yml` declares (`jira-issue-fetcher-todo` → running, `jira-issue-fetcher` → suspended), so to change a fetcher's default permanently edit `suspend` in its manifest.

## Agent runtime knobs (`agents-env` ConfigMap)

| Key | Default | Effect |
|---|---|---|
| `JIRA_ALLOW_STATUS_CHANGES` | `"false"` | When `"false"`, agents do NOT change Jira issue statuses (no "New" → "In Progress" on rebase/backport start, no "Release Pending" / "Closed" on verification finish) and the preliminary-testing agent does NOT set `Preliminary Testing = Pass` (that field admits the build into a compose, triggers erratum creation, and moves the issue to Integration). Flip to `"true"` to allow all of the above. `DRY_RUN=true` further short-circuits these writes independently. |

To enable production status transitions:

```bash
oc patch configmap agents-env --type merge -p '{"data":{"JIRA_ALLOW_STATUS_CHANGES":"true"}}'
oc rollout restart deployment -l app=triage-agent  # plus any other agent deployments
```

## Triggering the Pipeline Manually

To push a Jira issue into the triage queue (e.g. to force CVE triage):

```bash
oc rsh deployment/valkey redis-cli LPUSH triage_queue '{"metadata": {"issue": "RHEL-XXXXXX", "force_cve_triage": true}}'
```

Set `force_cve_triage` to `false` for a normal triage run. This mirrors the `make trigger-pipeline` target used locally.

## Inspecting queues and following logs

Run these from the `openshift/` directory (they shell into the `valkey` pod via `oc exec`, so an active `oc login` to the project is required).

### Queue contents

Each `show-*-queue` target lists the Jira issue keys currently waiting in a queue, oldest-first (next to be popped). Every input queue has a priority twin (`<queue>_todo`) that the agents drain first for `ymir_todo`-triggered tasks, so those targets print the `_todo` queue before the normal one.

```bash
make show-triage-queue          # triage_queue_todo + triage_queue
make show-rebase-queue-c9s      # rebase_queue_c9s_todo + rebase_queue_c9s
make show-rebase-queue-c10s     # rebase_queue_c10s_todo + rebase_queue_c10s
make show-backport-queue-c9s    # backport_queue_c9s_todo + backport_queue_c9s
make show-backport-queue-c10s   # backport_queue_c10s_todo + backport_queue_c10s
make show-rebuild-queue-c9s     # rebuild_queue_c9s_todo + rebuild_queue_c9s
make show-rebuild-queue-c10s    # rebuild_queue_c10s_todo + rebuild_queue_c10s
make show-clarification-queue   # clarification_needed_queue (no priority twin)
make show-error-list            # per-issue / per-tool-error breakdown via scripts/error_list.py
```

### Following pod logs

Each `logs-*` target follows (`oc logs -f`) the corresponding deployment's pod:

```bash
make logs-triage           # triage-agent
make logs-backport-c9s     # backport-agent-c9s
make logs-backport-c10s    # backport-agent-c10s
make logs-rebase-c9s       # rebase-agent-c9s
make logs-rebase-c10s      # rebase-agent-c10s
make logs-rebuild-c9s      # rebuild-agent-c9s
make logs-rebuild-c10s     # rebuild-agent-c10s
make logs-mcp              # mcp-gateway
make logs-supervisor       # supervisor-processor
make logs-valkey           # valkey
make logs-phoenix          # phoenix
make logs-redis-commander  # redis-commander
make logs-otel-collector   # otel-collector
```

## Image rebuilds of MCP Gateway and agent images

They are built internally. If you need a rebuild right now, head over
to [the Gitlab jobs view](https://gitlab.cee.redhat.com/jotnar-project/deployment/-/jobs?kind=BUILD)
and respin those that you need.

Otherwise they are rebuilt nightly at 3:00.
