# AI workflows

This repository contains the code for two different RHEL package maintenance workflows,
that use the same basic components (BeeAI framework, Redis, etc.),
but are implemented with different architectures.

The first workflow is [**Packaging Workflow**](README-agents.md) -
the goal of this workflow is to triage incoming issues for the RHEL project,
figure which ones are candidates for automatic resolution,
and create merge requests to merge them.

The second workflow is [**Testing and Release Workflow**](README-supervisor.md) workflow -
the goal of this workflow is that once a merge request is merged and we have a candidate build attached to the issue,
we want to move the issue and the associated erratum,
through testing and the remainder of the RHEL process to the point where the build is ready to be released.

The `goose/` directory contains some automation components that were used in the early stages of this project. These components are **mostly unmaintained** and preserved primarily for reference. For active development and production use, focus on the main workflows described above.

## Documentation

Data flow documentation for external service integrations:

- [Jira Data Flow](jira_data_flow.md) - Integration with Jira issue tracking
- [Jira Label-Based Workflow Routing](jira_label_workflow_routing.md) - Label state machine and queue routing
- [GitLab Dist-Git Data Flow](gitlab_distgit_data_flow.md) - CentOS Stream and RHEL dist-git repositories
- [Brew/Konflux Build System Data Flow](brew_konflux_data_flow.md) - Build system integration
- [AI Providers Data Flow](ai_providers_data_flow.md) - Google Vertex AI integration and model usage

## Development environment

You need to have the following packages installed on your system:

```
python3-devel podman-compose gcc krb5-devel
```

Then, use the provided stub pyproject.toml file to set up the development environment:

```
uv sync
uv run make -f Makefile.tests check
```

You'll also need to have `python3-rpm` installed on the host system -
the `rpm` module installed from PyPI is [rpm-shim](https://github.com/packit/rpm-shim)
and just pulls the files from python3-rpm into the venv.
In an IDE, select .venv/bin/python as the Python interpreter.
