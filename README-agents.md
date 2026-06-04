# Package Maintenance Workflows as AI agents

A set of AI agents implemented in the BeeAI Framework, interconnected via Redis, observed by Phoenix.
Every agent can run individually or pick up tasks from a Redis queue.

See [README.md](README.md) for general notes about setting up the development environment.

## Architecture

Three agents process tasks through Redis queues:
- **Triage Agent**: Analyzes JIRA issues and determines resolution path. It uses title, description, fields, and comments to find out root cause of the issue. It can ask for clarification, create tasks for other agents or may take no action if not needed.
- **Rebase Agent**: Updates packages to newer upstream versions. A Rebase is only to be chosen when the issue explicitly instructs you to "rebase" or "update". It looks for upstream references that are linked, attached and present in the description or comments in the issue.
- **Backport Agent**: Applies specific fixes/patches to packages. It looks for patches that are linked, attached and present in the description or comments in the issue. It tries to apply the patch and resolve any conflicts that may arise during the backport process.
- **Issue Verification Agent**: Manages the post-fix lifecycle of a JIRA issue — from merged MR through errata creation, testing analysis, and status transitions to RELEASE_PENDING. Migrated from the supervisor's `IssueHandler`.


## Dry run mode

**Without setting `DRY_RUN=true` env var, agents will make real changes:**
- **Modify JIRA issues** (add comments, update fields, apply labels)
- **Create GitLab merge requests** and push commits

**Always** use `DRY_RUN=true` if you are developing locally or just wanna give the agents a try.

## Jira status changes (opt-in)

By default, agents do NOT change the Jira workflow status of issues
(e.g. "New" → "In Progress" when the backport agent picks up a task,
or "Release Pending" / "Closed" when the issue-verification agent
finishes), and the preliminary-testing agent does NOT set the
`Preliminary Testing` field to `Pass`. The Pass field is gated by the
same flag because setting it admits the build into the next compose,
triggers erratum creation, and moves the issue to Integration — its
downstream effect is equivalent to a status transition. To enable all
of the above, set:

```bash
JIRA_ALLOW_STATUS_CHANGES=true
```

When unset or `false`, status-change calls and the prelim-testing Pass
write are short-circuited and a log line records what *would* have
happened.
`DRY_RUN=true` also suppresses these writes independently of this flag.

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

### FuSa (Functional Safety) Packages

The `ymir_fusa` label will be automatically added by the triage agent to JIRA issues involving FuSa packages. Related merge requests will need to be reviewed and approved by subject matter experts before merging.

## Troubleshooting

### Phoenix: Alembic migration failure after version rollback

When Phoenix is rolled back to an older version (e.g. v16 → v15), the SQLite database
contains an Alembic revision from the newer version that the older code doesn't
recognize. Phoenix crashes on startup with:

```
alembic.util.exc.CommandError: Can't locate revision identified by '0ff41b5b118f'
```

The fix is to stamp the database with the head revision the running Phoenix version
expects. For the v16 → v15 rollback, the known revisions are:

| Version | Alembic head revision |
|---------|-----------------------|
| v15     | `575aa27302ee`        |
| v16     | `0ff41b5b118f`        |

#### Fix locally (podman-compose)

1. Stop Phoenix:

   ```bash
   podman-compose stop phoenix
   ```

2. Find the volume mount path:

   ```bash
   podman volume inspect ai-workflows_phoenix-data --format '{{.Mountpoint}}'
   ```

3. Stamp the database:

   ```bash
   python3 -c "
   import sqlite3, sys
   db = '$(podman volume inspect ai-workflows_phoenix-data --format '{{.Mountpoint}}')/phoenix.db'
   conn = sqlite3.connect(db)
   cur = conn.cursor()
   cur.execute('SELECT version_num FROM alembic_version')
   print('Current:', cur.fetchall())
   cur.execute(\"UPDATE alembic_version SET version_num = '575aa27302ee' WHERE version_num = '0ff41b5b118f'\")
   print('Rows updated:', cur.rowcount)
   conn.commit()
   cur.execute('SELECT version_num FROM alembic_version')
   print('Updated:', cur.fetchall())
   conn.close()
   "
   ```

4. Start Phoenix again:

   ```bash
   podman-compose start phoenix
   ```

#### Fix in OpenShift

1. Scale down Phoenix to release the PVC:

   ```bash
   oc scale deployment phoenix --replicas=0
   ```

2. Find the head revision the current Phoenix image expects:

   ```bash
   oc debug deployment/phoenix --container=phoenix -- python3 -c "
   from alembic.config import Config
   from alembic.script import ScriptDirectory
   c = Config()
   c.set_main_option('script_location', '/usr/local/lib/python3.14/site-packages/phoenix/db/migrations')
   s = ScriptDirectory.from_config(c)
   print('Head revision:', s.get_current_head())
   "
   ```

3. Update the `alembic_version` in the SQLite database (replace `OLD_REV` and
   `NEW_REV` with the values from the table above, or use step 2 output for a
   different version pair):

   ```bash
   oc debug deployment/phoenix --container=phoenix -- python3 -c "
   import sqlite3
   conn = sqlite3.connect('/mnt/data/phoenix.db')
   cur = conn.cursor()
   cur.execute('SELECT version_num FROM alembic_version')
   print('Current:', cur.fetchall())
   cur.execute(\"UPDATE alembic_version SET version_num = 'NEW_REV' WHERE version_num = 'OLD_REV'\")
   print('Rows updated:', cur.rowcount)
   conn.commit()
   cur.execute('SELECT version_num FROM alembic_version')
   print('Updated:', cur.fetchall())
   conn.close()
   "
   ```

4. Scale Phoenix back up:

   ```bash
   oc scale deployment phoenix --replicas=1
   ```

The stamp only updates Alembic's tracking metadata — any extra columns or indexes
added by the newer version's migrations remain in the database. SQLite tolerates
unused columns, so this is generally safe.

## Advanced Usage

### Automatic Issue Fetching
```bash
# Setup automatic issue fetching from JIRA
cp templates/jira-issue-fetcher.env .secrets/jira-issue-fetcher.env
make build-jira-issue-fetcher
make run-jira-issue-fetcher
```
