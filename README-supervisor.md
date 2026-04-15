# Testing and Release Workflow

The supervisor that orchestrates the workflow of testing and releasing a build.

See [README.md](README.md) for general notes about setting up the development environment.

## Architecture

**Work Queue** - we store a queue in Redis of work items that need to be processed, ordered by the time that the work item needs to be processed.

**Work Item** - the basic unit of work is to examine an issue or erratum and figure out what needs to happen next. The result of processing a work item will typically to be one of:

 - Perform an action like firing off a set of tests or pushing the files of the erratum to staging.
 - Change the state of the issue or erratum
 - Flag the issue or erratum for human attention

 Sometimes no action will be performed, and the work item will be rescheduled to be run again later.

 If no further work is needed (because we've reached the final state of the workflow, or because the issue has been flagged for human attention) then the work item is removed from the work queue.

**Collector** - a service that runs periodically examines open issues and their associated errata, sees which need work, and adds work items for them to work queue.

**Processor** - a service that runs fetches ready work items from the work queue and processes them.

## Setup

Copy the `templates` directory to `.secrets` and fill in required information in `.secrets/supervisor.env`

To let the collector and processor access the Errata Tool API, you will also need to initialize a Kerberos ticket for it:

```
mkdir -p .secrets/ccache
sudo chown -R $(id -u):$(id -g) .secrets/ccache
kinit -c .secrets/ccache/krb5cc <username>@IPA.REDHAT.COM
```

This will have to be repeated when the ticket expires. (Exporting the kcm-cache
socket to the container seems better, but I wasn't able to get it to work due
to various difficult to work around permission issues.)

## Processing a single issue or erratum

To process a single issue or erratum, you can run:

```
make process-issue JIRA_ISSUE=RHEL-12345
make process-erratum ERRATA_ID=12345
```

This will process the work item in a one-off container,
and will not affect the work queue,
even if the result of processing the work item would normally be to reschedule the work item or remove it from the queue.

Additional variables are supported:

```
DRY_RUN=true # Don't actually make any changes to issues/errata, just show what would be done
DEBUG=true   # Use more detailed logging (shows exactly what would happen for dry run items)
```

## Testing the collector

You can test the collection process with:

```
make supervisor-clear-queue
make supervisor-collect
```

That will clear any existing queue items, collect items from scratch,
and log what gets added to the queue.
