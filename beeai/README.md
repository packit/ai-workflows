#  BeeAI workflows

This repository contains the code for two different maintenance workflows,
that use the same basic components (BeeAI framework, Redis, etc.),
but are implemented with different architectures.

The first workflow is that [**Merge Request Workflow**](README-agents.md) -
the goal of this workflow is to triage incoming issues for the RHEL project,
figure which ones are candidates for automatic resolution,
and create merge requests to merge them.

The second workflow is that [**Testing and Release Workflow**](README-supervisor.md) workflow -
the goal of this workflow is that once a merge request is merged and we have a candidate build attached to the issue,
we want to move the issue and the associated erratum,
through testing and the remainder of the RHEL process to the point where the build is ready to be released.

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
