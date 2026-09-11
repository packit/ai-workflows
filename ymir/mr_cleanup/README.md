# MR Cleanup

Daily cronjob with two phases for managing stale bot-authored GitLab MRs.

## Phases

**Phase 1 -- Close stale MRs** (`CLOSE_STALE_MRS=true`, default):
Closes open bot MRs that reference one or more closed Jira issues, even when
other referenced Jira issues are not reported as closed. The closing comment
identifies the closed and remaining Jira issues, and the script adds the
`ymir_cleaned_up` label. The comment explains how to retrigger Ymir with the
`ymir_todo` Jira label if the work is still needed. No Jira labels are modified
by this phase.

**Phase 2 -- Label closed-MR Jiras** (`RESET_CLOSED_MR_JIRAS=true`, default):
For closed (not merged) bot MRs, adds `ymir_mr_closed` to the referenced
Jiras. Existing automation labels (e.g. `ymir_backported`) are preserved
to maintain the historical trace for coverage metrics. Skips Jiras
referenced by an open MR or a merged MR (within a 180-day lookback window).

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
| `CLOSE_STALE_MRS` | `true` | Enable phase 1 |
| `RESET_CLOSED_MR_JIRAS` | `true` | Enable phase 2 |
| `GITLAB_BOT_AUTHORS` | `jotnar-bot,redhat-ymir-agent` | Comma-separated bot usernames to scan |

## Deployment

Runs as an OpenShift CronJob daily at 4am UTC. Deployed via `openshift/deploy.sh`
using credentials from existing `gitlab-env` and `jira-env` secrets.
