---
name: verify-prod-deployment
description: Use when the user says they did a fresh prod deployment and wants to know if everything is okay. Checks pods, deployments, and events on the jotnar-ymir OpenShift cluster.
---

# Verify Production Deployment

## Overview

Check the health of the jotnar-ymir OpenShift cluster after a fresh deployment. Run these commands in order and report findings.

## Steps

### 1. What changed

```bash
git log --oneline -25
```

### 2. Pod status

```bash
oc get pods -n jotnar-ymir--jotnar-ymir
```

**Expected:** All agent/service pods `Running`, completed jobs `Completed`, zero `Error` or `CrashLoopBackOff` or `ImagePullBackOff`.

### 3. Deployment rollout status

```bash
oc get deploy -n jotnar-ymir--jotnar-ymir
```

**Expected:** All deployments show `READY` count matches desired (e.g. `2/2`, `1/1`).

### 4. Imagestreams

```bash
oc get imagestream -n jotnar-ymir--jotnar-ymir
```

Note the `UPDATED` timestamp for each imagestream. In the report, call out any agent or `mcp-server` imagestream updated within the last 24 hours — these are the ones that just rolled out.

### 5. Recent events

```bash
oc get events -n jotnar-ymir--jotnar-ymir --sort-by='.lastTimestamp' | tail -40
```

Look for `Warning` severity lines — `Normal` is noise. Flag anything other than routine `Pulling/Pulled/Created/Started/Completed`.

## Reporting

Report as a short table or bullet list:

- All pods running: yes/no
- Any restarts: yes (pod name + count) / no
- Any warnings in events: yes (what) / no
- Deployments all READY: yes/no
- Imagestreams updated <24h: list each (name, tag, updated) — highlight agents and mcp-server
- Overall verdict: **OK** or **ISSUE: <what>**

## Known gotchas

- **Recreate strategy:** Deployments use `Recreate` (not RollingUpdate). During rollout there is a brief gap with 0 pods — normal, not a failure.
- **Quota deadlock:** If new pods show `FailedCreate` + quota error, check `oc get appliedclusterresourcequotas`. Usually resolves itself with Recreate strategy.
- **jira-issue-fetcher jobs:** These are cronjobs that run every 5 minutes and complete quickly — `Completed` status is expected and healthy.
