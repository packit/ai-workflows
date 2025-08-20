# BeeAI workflow

A set of AI agents implemented in the BeeAI Framework, interconnected via Redis.
Every agent can run individually or pick up tasks from a Redis queue.

## Architecture

Three agents process tasks through Redis queues:
- **Triage Agent**: Analyzes JIRA issues and determines resolution path
- **Rebase Agent**: Updates packages to newer upstream versions
- **Backport Agent**: Applies specific fixes/patches to packages

## Setup

Copy the `templates` directory to `.secrets` and fill in required information.

## Running as a service

The agents run continuously, waiting for work from Redis queues. To process a JIRA issue:

**Step 1: Start the system** (if not already running)
```bash
make start
```

This runs the services in the foreground, showing logs for monitoring and debugging. If you prefer to run the services in the background, use `make start-detached` instead.

**Step 2: Trigger work**
```bash
make trigger-pipeline JIRA_ISSUE=RHEL-12345
```

## Running individual agents

You can run any agent individually with the appropriate make target, passing required input data via environment variables, e.g. `make JIRA_ISSUE=RHEL-12345 run-triage-agent-standalone`.
The agent will run only once, print its output and exit.

```bash
make JIRA_ISSUE=RHEL-12345 run-triage-agent-standalone
make PACKAGE=httpd VERSION=2.4.62 JIRA_ISSUE=RHEL-12345 BRANCH=c10s run-rebase-agent-standalone
make PACKAGE=httpd UPSTREAM_FIX=https://github.com/... JIRA_ISSUE=RHEL-12345 BRANCH=c10s run-backport-agent-standalone
```

## Dry-Run mode

Both backport and rebase agents support **dry-run mode** for testing workflows without actually pushing changes or creating merge requests. By default, agents run in **production mode** and will create actual commits, pushes, and merge requests.

To enable dry-run mode for testing, set the `DRY_RUN=true` environment variable.

## Observability

You can connect to http://localhost:6006/ to access Phoenix web interface and trace agents
(it works with individual runs too).

Redis Commander is available at http://localhost:8081/ for monitoring of the queue.

## Development environment

A stub pyproject.toml is provided to set up a development environment:

```
cd beeai
uv sync
uv run make -f Makefile.tests check
```

You'll need to have `python3-rpm` installed on the host system -
the `rpm` module installed from PyPI is [rpm-shim](https://github.com/packit/rpm-shim)
and just pulls the files from python3-rpm into the venv.
In an IDE, select beeai/.venv/bin/python as the Python interpreter.
