# Agents as Skills

This directory contains Ymir workflows packaged as **AI coding assistant skills** for [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) and [Cursor](https://www.cursor.com/). The goal is to give individual contributors an easy way to run Ymir workflows directly in their own development environment — helping them with day-to-day package maintenance tasks and potentially surfacing areas for improvement in the workflows themselves.

## Available skills

| Skill | Directory | Description |
|-------|-----------|-------------|
| **Triage** | [`triage/`](triage/) | Triage CVE/bug Jira issues for RHEL packages |
| **Backport** | [`backport/`](backport/) | Cherry-pick or git-am upstream patches, verify builds, and create merge requests |
| **Rebase** | [`rebase/`](rebase/) | Rebase a package to a new upstream version |
| **Rebuild** | [`rebuild/`](rebuild/) | Rebuild a package in the build system |
| **Preliminary Testing** | [`preliminary_testing/`](preliminary_testing/) | Analyze gating and OSCI results to determine preliminary testing status |
| **Issue Verification** | [`issue_verification/`](issue_verification/) | Issue verification agent (post-fix lifecycle management) |

## Installation

For installation instructions (skill setup and MCP tool configuration), see the [Skills Installation Guide](https://github.com/packit/ai-workflows/blob/main/skills_installation.md).

## How to use

Triage JIRA issue:

```
Use the triage skill with jira_issue=RHEL-12345
```

Backport JIRA issue:

```
Use the backport skill with jira_issue=RHEL-12345
```

Set the `DRY_RUN` environment variable or add `dry_run=true` to your prompt to avoid any updates (e.g., updating JIRA, GitLab, etc.) when executing the workflow.

## How to build

```bash
claude --model claude-opus-4-6 --effort high "Take a look at the BeeAI workflows implemented in agents directory. Convert Workflow in {workflow_file} to Claude skill and save that skill to agents_as_skills directory.
Restrictions:
 - Pay attention to tools used by the workflow and do not omit them
 - Do not restrict tools that the skill can use
 - Specify arguments the skill uses as an input"
```
