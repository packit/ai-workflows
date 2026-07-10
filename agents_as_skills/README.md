# Agents as Skills

This directory contains Ymir workflows packaged as **AI agent skills** compatible with any client that implements the [Agent Skills standard](https://agentskills.io/home). The goal is to give individual contributors an easy way to run Ymir workflows directly in their own development environment — helping them with day-to-day package maintenance tasks and potentially surfacing areas for improvement in the workflows themselves.

Supported clients include [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview), [opencode](https://opencode.ai), [Cursor](https://www.cursor.com/), and any other [skills-compatible agent](https://agentskills.io/clients).

## Available skills

| Skill | Directory | Description |
|-------|-----------|-------------|
| **Triage** | [`triage/`](triage/) | Triage CVE/bug Jira issues for RHEL packages |
| **Backport** | [`backport/`](backport/) | Cherry-pick or git-am upstream patches, verify builds, and create merge requests |
| **Rebase** | [`rebase/`](rebase/) | Rebase a package to a new upstream version |
| **Rebuild** | [`rebuild/`](rebuild/) | Rebuild a package in the build system |
| **Preliminary Testing** | [`preliminary-testing/`](preliminary-testing/) | Analyze gating and OSCI results to determine preliminary testing status |
| **Issue Verification** | [`issue-verification/`](issue-verification/) | Post-fix lifecycle management through errata creation and testing |

Each skill is a directory containing a `SKILL.md` file that follows the [Agent Skills specification](https://agentskills.io/specification).

## Installation

Quick install with the interactive script (skills + MCP tools + credentials):

```bash
python3 agents_as_skills/install-skills.py
```

1. Choose your client (`cursor`, `claude`, or `opencode`)
2. Enter credentials when prompted (Jira, GitLab, Kerberos/keytab)
3. Restart the client and confirm the MCP servers are connected

For manual per-client setup (skill paths, MCP config), see the [Skills Installation Guide](../skills_installation.md).

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

To convert a Ymir BeeAI workflow into a new skill:

```bash
opencode "Please take a look at the BeeAI workflows implemented in the agents directory. Please convert the workflow in {workflow_file} to an Agent Skill (https://agentskills.io/specification) and save it to agents_as_skills/. Restrictions:
 - Pay attention to tools used by the workflow and do not omit them
 - Do not restrict tools that the skill can use
 - Include a name: field in the frontmatter that matches the directory name"
```
