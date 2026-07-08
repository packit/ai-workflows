# MR Cleanup

Daily cronjob that closes stale GitLab merge requests created by Ymir bots
(`jotnar-bot`, `redhat-ymir-agent`) when all referenced Jira issues have been closed.

## How it works

1. Fetches all open bot-authored MRs in `redhat/rhel/rpms` and `redhat/centos-stream/rpms`
2. Extracts Jira keys from commit messages (`Resolves: RHEL-NNNNN`)
3. Batch-queries Jira for issue statuses
4. For each MR:
   - **All Jiras closed** -- posts a closing comment, closes the MR, adds `ymir_cleaned_up` label
   - **Any Jiras still open** -- skips
   - **Already has `ymir_cleaned_up` label** -- skips (prevents re-closing reopened MRs)

No Jira labels are modified -- metrics dashboards depend on them remaining in place.

## Setup

```bash
cp templates/mr-cleanup.env .secrets/mr-cleanup.env
# Edit with your GitLab token and Jira credentials
```

## Usage

```bash
# Build the image
make build-mr-cleanup

# Dry run -- lists what would be closed without making changes
make run-mr-cleanup-dry-run

# Live run -- closes MRs and posts comments
make run-mr-cleanup

# Target a single MR (dry run)
TARGET_MR=https://gitlab.com/redhat/rhel/rpms/foo/-/merge_requests/1 make run-mr-cleanup-dry-run

# Target a single MR (live)
TARGET_MR=https://gitlab.com/redhat/rhel/rpms/foo/-/merge_requests/1 make run-mr-cleanup
```

## Deployment

Runs as an OpenShift CronJob daily at 4am UTC. Deployed via `openshift/deploy.sh`
using credentials from existing `gitlab-env` and `jira-env` secrets.
