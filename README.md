# AI workflows

## Overview

**Jötnar** is an AI-powered RHEL package maintenance automation system that triages Jira issues and creates merge requests for package rebases and backports.

### Business Purpose
The AI Workflows system automates RHEL package maintenance by triaging incoming Jira issues to determine if they can be automatically resolved through rebases or backports, then creating merge requests to the appropriate dist-git repositories. Once merge requests are merged and candidate builds are created, the system manages the testing and release workflow, moving builds through validation and the RHEL release process until they are ready for production deployment.

### Key Features
- **Automated Triage**: AI analyzes Jira issues to determine appropriate workflow (rebase vs backport)
- **Patch Selection**: AI selects and applies upstream patches to spec files
- **Build Testing**: Automated Copr builds with failure diagnosis
- **MR Creation**: Automatic merge request creation with detailed checklists
- **Release Management**: Automated progression through RHEL testing and release pipeline

### AI Technology
- **Powered by**: models via Vertex AI
- **Framework**: BeeAI agent orchestration framework
- **Observability**: Phoenix tracing for AI model monitoring

### ⚠️ Limitations & Risks
- **AI can make mistakes**: The model may select incorrect patches, miss dependencies, or introduce build failures
- **Hallucinations possible**: AI-generated commit messages or changelog entries may be inaccurate
- **Incomplete backports**: Multi-commit fixes may be partially backported
- **Security considerations**: Always verify patches don't introduce vulnerabilities
- **Human review required**: All AI-generated MRs must be reviewed for accuracy and security before merging
For reporting issues, please refer to the "Contact & Feedback" section below.
## Workflows
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

**Data flow documentation for external service integrations:**

- [Jira Data Flow](jira_data_flow.md) - Integration with Jira issue tracking
- [Jira Label-Based Workflow Routing](jira_label_workflow_routing.md) - Label state machine and queue routing
- [GitLab Dist-Git Data Flow](gitlab_distgit_data_flow.md) - CentOS Stream and RHEL dist-git repositories
- [Brew/Konflux Build System Data Flow](brew_konflux_data_flow.md) - Build system integration
- [AI Providers Data Flow](ai_providers_data_flow.md) - Google Vertex AI integration and model usage

**Data management:**

- [Data Retention Policy](data_retention_policy.md) - Retention periods for logs, queues, and temporary data

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

## Contact & Feedback

**Questions or issues with Jötnar?**

- **Email**: jotnar@redhat.com
- **Slack**: #forum-jötnar-package-automation
- **Report AI Issues**: [Jira Issues](https://issues.redhat.com/) (project: Packit, component: jotnar) or [GitHub Issues](https://github.com/packit/ai-workflows/issues)

  If you encounter incorrect backports, hallucinations, or other AI-related problems, please file a Jira or GitHub issue.
