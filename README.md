# AI workflows

## Overview

This repository is an AI-powered RHEL package maintenance automation system (codename "Ymir", formerly "Jötnar") that triages Jira issues, creates merge requests for package rebases and backports, and can handle quality processes.

> **Note on naming:** The project used the codename "Jötnar" during the pilot phase. "Ymir" is the name of our working group, and we adopted it as the project name to avoid confusion with other working groups. Some internal references still use "jotnar" (e.g. service accounts, bot usernames, Kerberos principals, container registry namespace) as those have not been migrated yet.

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

This project previously used [Goose](https://github.com/block/goose) as an AI agent framework in its early stages. The `goose/` directory and related automation have since been removed. For active development and production use, focus on the main workflows described above.

## Agents as Skills

The [`agents_as_skills/`](agents_as_skills/) directory contains Ymir workflows packaged as **AI coding assistant skills** for [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) and [Cursor](https://www.cursor.com/). The goal is to give individual contributors an easy way to run Ymir workflows directly in their own development environment — helping them with day-to-day package maintenance tasks and potentially surfacing areas for improvement in the workflows themselves.

Available skills:

| Skill | Description |
|-------|-------------|
| **Triage** | Triage CVE/bug Jira issues for RHEL packages |
| **Backport** | Cherry-pick or git-am upstream patches, verify builds, and create merge requests |
| **Rebase** | Rebase a package to a new upstream version |
| **Rebuild** | Rebuild a package in the build system |
| **Preliminary Testing** | Analyze gating and OSCI results to determine preliminary testing status |

For installation instructions (skill setup and MCP tool configuration), see the [Skills Installation Guide](skills_installation.md).

## Documentation

**Data flow documentation for external service integrations:**

- [Jira Data Flow](jira_data_flow.md) - Integration with Jira issue tracking
- [Jira Label-Based Workflow Routing](jira_label_workflow_routing.md) - Label state machine and queue routing
- [GitLab Dist-Git Data Flow](gitlab_distgit_data_flow.md) - CentOS Stream and RHEL dist-git repositories
- [Brew/Konflux Build System Data Flow](brew_konflux_data_flow.md) - Build system integration
- [AI Providers Data Flow](ai_providers_data_flow.md) - Google Vertex AI integration and model usage
- [Agent Monitoring and Performance Review](monitoring.md) - Monitoring processes, anomaly detection, and continuous improvement
- [MR Consolidation Architecture](docs/mr_consolidation_architecture.md) - Merging multiple backport MRs into a single MR

**Data management:**

- [Data Retention Policy](data_retention_policy.md) - Retention periods for logs, queues, and temporary data

## Development environment

You need to have the following packages installed on your system:

```
python3-devel podman-compose gcc krb5-devel libpq-devel
```

Then, use the provided stub pyproject.toml file to set up the development environment:

```
uv sync --extra test
uv run make -f Makefile.tests check
```

You'll also need to have `python3-rpm` installed on the host system -
the `rpm` module installed from PyPI is [rpm-shim](https://github.com/packit/rpm-shim)
and just pulls the files from python3-rpm into the venv.
In an IDE, select .venv/bin/python as the Python interpreter.

### Building with internal repos

Some tools are only available from internal repos that cannot be committed
to this upstream repository. To include them in your local container builds:

1. Copy the template: `cp -n templates/build.env .env` (or append its contents to your existing `.env` file)
2. Fill in the internal repo URLs and package names in `.env`
3. Run `make build` or `podman-compose build` — podman-compose automatically
   loads `.env` and passes the values as build args to the Containerfiles.

Without `.env`, containers build normally but without any
internal-only packages.

## Contact & Feedback

**Questions or issues with the agents?**

- **Email**: redhat-ymir-agent@redhat.com
- **Slack**: #forum-ymir-package-automation
- **Report AI Issues**: [Jira Issues](https://issues.redhat.com/) (project: Packit, component: jotnar) or [GitHub Issues](https://github.com/packit/ai-workflows/issues)

  If you encounter incorrect backports, hallucinations, or other AI-related problems, please file a Jira or GitHub issue.
