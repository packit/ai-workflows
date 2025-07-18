# BeeAI workflow

A set of AI agents implemented in the BeeAI Framework, interconnected via Redis.
Every agent can run individually or pick up tasks from a Redis queue.

## Setup

Copy the `templates` directory to `.secrets` and fill in required information.

## Running the workflow

Start the agents using e.g. `make run-triage-agent` or `make run-triage-agent-detached`. This will automatically
start all the related services. There is currently no mechanism to initiate the workflow other than manually
pushing a task to the `triage_queue` Redis list. The easiest way to do that is with:

```bash
podman compose exec valkey -- redis-cli lpush triage_queue '{"metadata":{"issue":"RHEL-12345"}}'
```

## Running individual agents

You can run any agent individually with the appropriate make target, passing required input data
via environment variables, e.g. `make JIRA_ISSUE=RHEL-12345 run-triage-agent-standalone`.
The agent will run only once, print its output and exit.

## Observability

You can connect to http://localhost:6006/ to access Phoenix web interface and trace agents
(it works with individual runs too).
