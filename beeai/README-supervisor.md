# Testing and Release Workflow

The supervisor that orchestrates the workflow of testing and releasing a build.

See [README.md](README.md) for general notes about setting up the development environment.

## Architecture

**Task Queue** - we store a queue in Redis of tasks that need to be processed, ordered by the time that the task needs to be performed.

**Task** - the basic unit of work is to examine an issue or erratum and figure out what needs to happen next. The result of a task will typically to be one of:

 - Perform an action like firing off a set of tests or pushing the files of the errata to staging.
 - Change the state of the issue or errata
 - Flag the issue or errata for human attention

 Sometimes no action will be performed, and the task will simply reschedule itself to be run again later.

 If no further work is needed (because we've reached the final state of the workflow, or because the issue has been flagged for human attention) then the task is removed from the task queue.

**Collector** - a service that runs periodically examines open issues and their associated errata, sees which need work, and adds tasks for them to the task queue.

**Executor** - a service that runs fetches ready tasks from the task queue and executes them.

## Setup

Copy the `templates` directory to `.secrets` and fill in required information in `.secrets/supervisor.env`

To let the collector and executor access the Errata Tool API, you will also need to initialize a Kerberos ticket for it:

```
mkdir -p .secrets/ccache
sudo chown -R $(id -u):$(id -g) .secrets/ccache
kinit -c .secrets/ccache/krb5cc <username>@IPA.REDHAT.COM
```

This will have to be repeated when the ticket expires. (Exporting the kcm-cache
socket to the container seems better, but I wasn't able to get it to work due
to various difficult to work around permission issues.)

## Processing a single task or erratum

To process a single issue or erratum, you can run:

```
make process-issue JIRA_ISSUE=RHEL-12345
make process-erratum ERRATA_ID=12345
```

This will run the task in a one-off container,
and will not affect the task queue,
even if the result of the task would normally be to reschedule the task or remove it from the queue.

## Starting the collector and executor

```
make supervisor-start
```

This runs the services in the foreground, showing logs for monitoring and debugging. If you prefer to run the services in the background, use `make supervisor-start-detached` instead.
