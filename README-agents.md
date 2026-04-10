# Package Maintenance Workflows as AI agents

A set of AI agents implemented in the BeeAI Framework, interconnected via Redis, observed by Phoenix.
Every agent can run individually or pick up tasks from a Redis queue.

See [README.md](README.md) for general notes about setting up the development environment.

## Architecture

Three agents process tasks through Redis queues:
- **Triage Agent**: Analyzes JIRA issues and determines resolution path. It uses title, description, fields, and comments to find out root cause of the issue. It can ask for clarification, create tasks for other agents or may take no action if not needed.
- **Rebase Agent**: Updates packages to newer upstream versions. A Rebase is only to be chosen when the issue explicitly instructs you to "rebase" or "update". It looks for upstream references that are linked, attached and present in the description or comments in the issue.
- **Backport Agent**: Applies specific fixes/patches to packages. It looks for patches that are linked, attached and present in the description or comments in the issue. It tries to apply the patch and resolve any conflicts that may arise during the backport process.


## Dry run mode

**Without setting `DRY_RUN=true` env var, agents will make real changes:**
- **Modify JIRA issues** (add comments, update fields, apply labels)
- **Create GitLab merge requests** and push commits

**Always** use `DRY_RUN=true` if you are developing locally or just wanna give the agents a try.

## Jira mocking

If you clone testing Jira files from
`git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git`
you can use them to work with pre-downloaded jira content instead of real Jira server.

Example:

`make run-triage-agent-standalone JIRA_ISSUE=RHEL-15216 MOCK_JIRA=true`

If used together with `DRY_RUN`, the agents won't edit the Jira files,
otherwise they will.

Example:

`make run-triage-agent-standalone JIRA_ISSUE=RHEL-15216 DRY_RUN=true MOCK_JIRA=true`

## Setup

### Required API Tokens & Authentication

- Copy templates:

```bash
cp -r templates .secrets
```

- Follow the comments to fill out the needed credentials.

The `mcp-gateway` requires a `keytab` file.

Steps to generate a personal kerberos keytab file:

```
> kinit <REDHAT_KERBEROS_ID>@IPA.REDHAT.COM

> kvno krbtgt/IPA.REDHAT.COM@IPA.REDHAT.COM
krbtgt/IPA.REDHAT.COM@IPA.REDHAT.COM: kvno = 1 # Note `kvno` value for the next step

> ktutil
ktutil:  addent -password -p <REDHAT_KERBEROS_ID>@IPA.REDHAT.COM -k 1 -f
Password for <REDHAT_KERBEROS_ID>@IPA.REDHAT.COM:
ktutil:  wkt /tmp/keytab
ktutil:  q

> kinit -kt /tmp/keytab <REDHAT_KERBEROS_ID>@IPA.REDHAT.COM
```

If last command is successful, move `/tmp/keytab` file to `.secrets/keytab` and set permissions to `644` so the user inside the container can read it.

This file should be kept secure as it can be used as a replacement for password-less authentication and impersonate the user.

To be able to access internal RHEL dist-git with your identity, update the `User` field in `files/internal_dist-git_ssh.conf` to match your `<REDHAT_KERBEROS_ID>` used in the keytab.

## Running the System

Please do not run `podman-compose up` directly; use the provided Makefile instead.

### Full Pipeline (Production)
```bash
# Start all agents and services
make start

# With options:
make start DRY_RUN=true                    # Skip Jira writes and git pushes
make start AUTO_CHAIN=false                # Disable downstream queue routing (triage only)
make start DRY_RUN=true AUTO_CHAIN=false   # Combine both

# Process a JIRA issue
make trigger-pipeline JIRA_ISSUE=RHEL-12345

# Force triage of Y-stream CVEs (normally skipped)
make trigger-pipeline JIRA_ISSUE=RHEL-12345 FORCE_CVE_TRIAGE=true
```

**Environment variables:**
| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `false` | Skip Jira writes, git pushes, and MR creation |
| `AUTO_CHAIN` | `true` | Route triaged issues to downstream backport/rebase queues. Set to `false` to disable routing. |
| `MOCK_JIRA` | `false` | Use mock Jira API instead of real Jira |
| `JIRA_DRY_RUN` | `false` | Skip all Jira write operations (status, comments, labels, fields) while keeping reads working |
| `FORCE_CVE_TRIAGE` | `false` | Force triage of CVE issues that would normally be skipped (e.g. Y-stream CVEs) |

### Individual Agents Runs
```bash
# Test specific agents standalone
make JIRA_ISSUE=RHEL-12345 run-triage-agent-standalone
make PACKAGE=httpd VERSION=2.4.62 JIRA_ISSUE=RHEL-12345 BRANCH=c10s run-rebase-agent-standalone
make PACKAGE=httpd UPSTREAM_PATCHES=https://github.com/... JIRA_ISSUE=RHEL-12345 BRANCH=c10s run-backport-agent-standalone
make PACKAGE=httpd JIRA_ISSUE=RHEL-12345 BRANCH=c10s run-rebuild-agent-standalone

# Or with dry-run
DRY_RUN=true make JIRA_ISSUE=RHEL-12345 run-triage-agent-standalone

# Force triage of a Y-stream CVE
make JIRA_ISSUE=RHEL-12345 FORCE_CVE_TRIAGE=true run-triage-agent-standalone
```

Use commas to delimit multiple patch/commit URLs in `UPSTREAM_PATCHES`.

**Monitoring:**
- Phoenix tracing: http://localhost:6006/
- Redis queue monitoring: http://localhost:8081/

## How It Works

For detailed information about queue routing, label state transitions, and workflow diagrams, see the [Jira Label-Based Workflow Routing](jira_label_workflow_routing.md) documentation.

### Service triggering

Ymir bot processes issues assigned to `jotnar-project`.

Issues can be re-triggered through the workflow in two ways:
1. **Remove any existing `ymir_*` label** - allows the issue to re-enter the system on the next fetcher run
2. **Add the `ymir_retry_needed` label** - triggers workflow retry. Use cases include:
   - Package maintainers who have made changes (e.g., updated some fields, added links, commented)
   - Ymir team members after production code updates to resolve issues

### Maintainer Review Process

Some Jira issues will require a maintainer review by applying the `ymir_needs_maintainer_review` label to an issue. This is currently agreed on for FuSa (Functional Safety) project packages.

The `ymir_fusa` label will be automatically added by the triage agent to JIRA issues involving FuSa packages, and related merge requests will need to be reviewed and handled by subject matter experts.

## Advanced Usage

### Automatic Issue Fetching
```bash
# Setup automatic issue fetching from JIRA
cp templates/jira-issue-fetcher.env .secrets/jira-issue-fetcher.env
make build-jira-issue-fetcher
make run-jira-issue-fetcher
```
