# MR Cleanup

Daily cronjob with two phases for managing stale bot-authored GitLab MRs.

## Phases

**Phase 1 -- Close stale MRs** (`CLOSE_STALE_MRS=true`, default):
Closes open bot MRs whose referenced Jira issues have all been closed.
Posts a closing comment, adds `ymir_cleaned_up` label. No Jira labels modified.

**Phase 2 -- Reset Jira labels** (`RESET_CLOSED_MR_JIRAS=true`, default):
For closed (not merged) bot MRs, removes `ymir_*` automation outcome
labels from the referenced Jiras and adds `ymir_mr_closed`. Skips Jiras still
referenced by an open MR.

## Setup

```bash
cp templates/mr-cleanup.env .secrets/mr-cleanup.env
# Edit with your GitLab token and Jira credentials
```

## Usage

```bash
# Build the image
make build-mr-cleanup

# Dry run -- lists what would be changed without making changes
make run-mr-cleanup-dry-run

# Live run
make run-mr-cleanup

# Target a single MR (dry run)
TARGET_MR=https://gitlab.com/redhat/rhel/rpms/foo/-/merge_requests/1 make run-mr-cleanup-dry-run

# Phase 2 only with a different bot account (e.g. sustaining engineering)
CLOSE_STALE_MRS=false GITLAB_BOT_AUTHORS=rhel-se-jotnar-admin make run-mr-cleanup-dry-run
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GITLAB_TOKEN` | (required) | GitLab API token with `api` scope |
| `JIRA_URL` | (required) | Jira instance URL |
| `JIRA_EMAIL` | (required) | Jira account email |
| `JIRA_TOKEN` | (required) | Jira API token |
| `DRY_RUN` | `false` | Log what would change without making changes |
| `TARGET_MR` | | Process only this MR URL |
| `CLOSE_STALE_MRS` | `true` | Enable phase 1 |
| `RESET_CLOSED_MR_JIRAS` | `true` | Enable phase 2 |
| `GITLAB_BOT_AUTHORS` | `jotnar-bot,redhat-ymir-agent` | Comma-separated bot usernames to scan |

## Deployment

Runs as an OpenShift CronJob daily at 4am UTC. Deployed via `openshift/deploy.sh`
using credentials from existing `gitlab-env` and `jira-env` secrets.
