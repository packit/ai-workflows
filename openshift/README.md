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

  `log-detective-env`:
  ```
  LOG_DETECTIVE_TOKEN
  LOG_DETECTIVE_URL
  ```
  Values from Bitwarden - jotnar group - Log Detective Production Credentials (token = password field, URL = notes field).
  ```bash
  # Add to .secrets/beeai-agent.env (values from Bitwarden, see above):
  #   LOG_DETECTIVE_TOKEN=<password field>
  #   LOG_DETECTIVE_URL=<notes field>
  source .secrets/beeai-agent.env
  oc create secret generic log-detective-env \
    --from-literal=LOG_DETECTIVE_TOKEN=$LOG_DETECTIVE_TOKEN \
    --from-literal=LOG_DETECTIVE_URL=$LOG_DETECTIVE_URL
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

## Triggering the Pipeline Manually

To push a Jira issue into the triage queue (e.g. to force CVE triage):

```bash
oc rsh deployment/valkey redis-cli LPUSH triage_queue '{"metadata": {"issue": "RHEL-XXXXXX", "force_cve_triage": true}}'
```

Set `force_cve_triage` to `false` for a normal triage run. This mirrors the `make trigger-pipeline` target used locally.

## Image rebuilds of MCP Gateway and agent images

They are built internally. If you need a rebuild right now, head over
to [the Gitlab jobs view](https://gitlab.cee.redhat.com/jotnar-project/deployment/-/jobs?kind=BUILD)
and respin those that you need.

Otherwise they are rebuilt nightly at 3:00.
