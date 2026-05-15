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

## Installation

For installation instructions (skill setup and MCP tool configuration), see the [Skills Installation Guide](https://github.com/packit/ai-workflows/blob/main/skills_installation.md).

## How to build

```bash
claude --model claude-opus-4-6 --effort high "Please take a look at the BeeAI workflows implemented in agents directory. Please convert Workflow in {workflow_file} to Claude skill and save that skill to agents_as_skills directory.
Restrictions:
 - Pay attention to tools used by the workflow and do not omit them
 - Do not restrict tools that the skill can use
 - Specify arguments the skill uses as an input"
```
