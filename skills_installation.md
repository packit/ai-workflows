# Ymir Skills -- Workstation Installation Guide

This guide covers two things:

1. **Installing skills** from the `agents_as_skills/` directory so your AI
   coding assistant (Claude Code or Cursor) can use them.
2. **Installing the MCP tools** (`ymir-common` and `ymir-tools`) that the
   skills depend on, and configuring Claude Code to start both the
   **privileged** and **unprivileged** MCP gateways automatically.

---

## Installing skills

The `agents_as_skills/` directory in this repository contains ready-to-use
skills. Each sub-directory (e.g. `backport/`, `rebase/`, `triage/`) holds a
`SKILL.md` file that your editor picks up once it is placed in the correct
location.

### Available skills

| Skill | Directory | Description |
|-------|-----------|-------------|
| Backport | `agents_as_skills/backport/` | Cherry-pick or git-am upstream patches, verify builds, and create merge requests |
| Rebase | `agents_as_skills/rebase/` | Rebase a package to a new upstream version |
| Triage | `agents_as_skills/triage/` | Triage CVE/bug JIRA issues for RHEL packages |
| Rebuild | `agents_as_skills/rebuild/` | Rebuild a package in the build system |
| Preliminary Testing | `agents_as_skills/preliminary_testing/` | Run preliminary tests on a package |
| Issue Verification | `agents_as_skills/issue_verification/` | Issue verification agent (post-fix lifecycle management) |

### Claude Code

Claude Code discovers skills from `~/.claude/skills/`. Each skill is a
directory containing a single `SKILL.md` file. You can download them straight
from GitHub -- no need to clone the repository:

```bash
REPO_URL="https://raw.githubusercontent.com/packit/ai-workflows/main/agents_as_skills"

# Install all skills at once
for skill in backport rebase triage rebuild preliminary_testing; do
  mkdir -p ~/.claude/skills/"$skill"
  curl -fsSL "$REPO_URL/$skill/SKILL.md" -o ~/.claude/skills/"$skill"/SKILL.md
done
```

Or install a single skill:

```bash
REPO_URL="https://raw.githubusercontent.com/packit/ai-workflows/main/agents_as_skills"
mkdir -p ~/.claude/skills/backport
curl -fsSL "$REPO_URL/backport/SKILL.md" -o ~/.claude/skills/backport/SKILL.md
```

After downloading, restart Claude Code (or start a new session) so that the
skills are picked up.

### Cursor

Cursor discovers skills from `~/.cursor/skills-cursor/`. The same approach
applies:

```bash
REPO_URL="https://raw.githubusercontent.com/packit/ai-workflows/main/agents_as_skills"

# Install all skills at once
for skill in backport rebase triage rebuild preliminary_testing issue_verification; do
  mkdir -p ~/.cursor/skills-cursor/"$skill"
  curl -fsSL "$REPO_URL/$skill/SKILL.md" -o ~/.cursor/skills-cursor/"$skill"/SKILL.md
done
```

After downloading, restart Cursor so that the new skills appear in the skill
list.

---

## MCP Tools Installation

The skills listed above require the Ymir MCP tools to be installed and
running. The rest of this guide covers that setup.

## Overview

| Gateway | Module | Tools |
|---------|--------|-------|
| **Privileged** | `ymir.tools.privileged.gateway` | GitLab, Jira, Copr, Koji/Kerberos, lookaside cache, dist-git branching |
| **Unprivileged** | `ymir.tools.unprivileged.gateway` | Filesystem, shell, specfile, git operations, patches, upstream search, z-stream search |

Claude Code starts both servers in `stdio` mode and communicates with them
directly. Both run as child processes managed by Claude Code and are available
simultaneously throughout the session.

## Prerequisites

- Python 3.13 (`python3.13` package) -- the BeeAI framework used by the tools
  does not support Python >= 3.14 yet, and Fedora 43+ ships Python 3.14 as the
  default. Install Python 3.13 explicitly and create a virtual environment.
- Kerberos client tools (`kinit`, `klist`) -- required for Koji and dist-git operations
- System RPM bindings (`rpm` Python package)
- A valid `rhel-config.json` file (template provided in `templates/rhel-config.json`)

## 1. Install packages

```bash
sudo dnf install python3.13 krb5-devel gcc python3.13-devel
```

Create a dedicated virtual environment with Python 3.13.

```bash
python3.13 -m venv ~/.local/share/ymir-venv
```

Install `ymir-common` first because `ymir-tools` depends on it and the package
is not published on PyPI.

```bash
~/.local/share/ymir-venv/bin/pip install "git+https://github.com/packit/ai-workflows.git#subdirectory=ymir/common"
~/.local/share/ymir-venv/bin/pip install "git+https://github.com/packit/ai-workflows.git#subdirectory=ymir/tools"
```

After installation, two console scripts are available inside the virtual
environment:

```
~/.local/share/ymir-venv/bin/ymir-privileged-gateway
~/.local/share/ymir-venv/bin/ymir-unprivileged-gateway
```

## 2. Prepare `rhel-config.json`

Several tools (`BuildPackageTool`, `CheckCveTriageEligibilityTool`,
`VersionMapperTool`) load `rhel-config.json` from the current working
directory at runtime. Copy the template and fill in the real values:

```bash
cp templates/rhel-config.json ./rhel-config.json
# Edit ./rhel-config.json with actual RHEL stream data
```

Set the working directory in the Claude Code server configuration (see below)
to the directory that contains this file.

## 3. Environment variables

### Privileged gateway

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_TRANSPORT` | **Yes** | Set to `stdio` so Claude Code can communicate with the process. |
| `GITLAB_TOKEN` | **Yes** | GitLab API personal access token. |
| `JIRA_URL` | **Yes** | Base URL of the Jira instance (e.g. `https://issues.redhat.com/`). |
| `JIRA_EMAIL` | **Yes** | Email address for Jira Basic Auth. |
| `JIRA_TOKEN` | **Yes** | Jira API token for Basic Auth. |
| `GIT_REPO_BASEPATH` | No | Absolute path to a directory for git repository housekeeping. Only required if you use `CloneRepositoryTool` (it cleans up stale clones older than 14 days from this directory on every clone). |
| `KRB5CCNAME` | **Yes** | Kerberos credential cache location (e.g. `FILE:/tmp/krb5cc_1000`). |
| `KEYTAB_FILE` | No | Path to a Kerberos keytab for automated `kinit`. When unset, an existing ticket must be present. |
| `DRY_RUN` | No | Set to `true` to skip mutating Jira and lookaside operations. |
| `MOCK_JIRA` | No | Set to `true` to use a mock Jira client (for testing). |
| `JIRA_MOCK_FILES` | No | Directory with mock Jira JSON fixtures (only when `MOCK_JIRA=true`). |
| `SKIP_SETTING_JIRA_FIELDS` | No | Set to `true` to skip setting Jira custom fields. |
| `DEBUG_FILE` | No | Path to a log file. When set, gateway logs are written to this file in addition to stderr. Useful for debugging local installations. |

### Unprivileged gateway

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_TRANSPORT` | **Yes** | Set to `stdio` so Claude Code can communicate with the process. |
| `UPSTREAM_SEARCH_API_URL` | **Yes** | Base URL of the upstream search service (provides `/find_repository` and `/find_commit` endpoints). |
| `MCP_GATEWAY_URL` | No | SSE URL of a running privileged gateway. See the note on cross-gateway calls below. |
| `DEBUG_FILE` | No | Path to a log file. When set, gateway logs are written to this file in addition to stderr. Useful for debugging local installations. |
| `REQUESTS_CA_BUNDLE` | No | Path to a Certificate Authority (CA) bundle, if additional or custom ones are required for workflow services. |

## 4. Configure Claude Code

Add both servers to your Claude Code MCP configuration. You can do this
with the CLI or by editing the settings file directly.

### Option A -- Using `claude mcp add`

```bash
VENV="$HOME/.local/share/ymir-venv"

claude mcp add ymir-privileged \
  --env MCP_TRANSPORT=stdio \
  --env GITLAB_TOKEN=<your-gitlab-token> \
  --env JIRA_URL=https://redhat.atlassian.net \
  --env JIRA_EMAIL=you@redhat.com \
  --env JIRA_TOKEN=your-jira-api-token \
  --env KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) \
  -- "$VENV/bin/ymir-privileged-gateway"

claude mcp add ymir-unprivileged \
  --env MCP_TRANSPORT=stdio \
  --env UPSTREAM_SEARCH_API_URL=http://upstream-search.hosted.upshift.rdu2.redhat.com:80/v1 \
  --env REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
  -- "$VENV/bin/ymir-unprivileged-gateway"
```

### Option B -- Editing `~/.claude.json`

Add the following to the top-level `mcpServers` object:

Replace `<your-home>` with the absolute path to your home directory
(e.g. `/home/you`). The `~` shorthand is **not** expanded inside JSON values.

```json
{
  "mcpServers": {
    "ymir-privileged": {
      "command": "<your-home>/.local/share/ymir-venv/bin/ymir-privileged-gateway",
      "env": {
        "MCP_TRANSPORT": "stdio",
        "GITLAB_TOKEN": "<your-gitlab-token>",
        "JIRA_URL": "https://redhat.atlassian.net",
        "JIRA_EMAIL": "you@redhat.com",
        "JIRA_TOKEN": "your-jira-api-token",
        "KRB5CCNAME": "FILE:/tmp/krb5cc_1000",
        "LOG_DETECTIVE_URL": "https://logdetective-placeholder-server.com/",
        "LOG_DETECTIVE_TOKEN": "your-log-detective-api-token"
      }
    },
    "ymir-unprivileged": {
      "command": "<your-home>/.local/share/ymir-venv/bin/ymir-unprivileged-gateway",
      "env": {
        "MCP_TRANSPORT": "stdio",
        "UPSTREAM_SEARCH_API_URL": "http://upstream-search.hosted.upshift.rdu2.redhat.com:80/v1",
        "REQUESTS_CA_BUNDLE": "/etc/pki/tls/certs/ca-bundle.crt"
      }
    }
  }
}
```

For project-scoped configuration, place the same `mcpServers` block in
`.claude/settings.json` inside your project directory instead. If that
does not work for you, use the `.mcp.json` at the root of your project.

### Note: Kerberos keytab

If you use a keytab for automated Kerberos authentication, add `KEYTAB_FILE`
to the privileged server's `env`:

```json
"KEYTAB_FILE": "/path/to/your.keytab"
```

Without a keytab, you must run `kinit` manually before launching Claude Code
so that a valid ticket exists in the cache pointed to by `KRB5CCNAME`.

If you are using `KEYRING` credential cache (used by Kerberos by default),
you do not need to set `KRB5CCNAME`. All you need is a successful `kinit`.

## 5. Verify the setup

Launch Claude Code and confirm both servers are running:

```
/mcp
```

This lists all connected MCP servers and their available tools. You should see
tools from both `ymir-privileged` and `ymir-unprivileged` servers.

## Network topology

```
Claude Code
  |
  |-- stdio --> ymir-privileged (child process)
  |               |-- GitLab API        (GITLAB_TOKEN)
  |               |-- Jira API          (JIRA_EMAIL + JIRA_TOKEN)
  |               |-- Copr / Koji       (Kerberos)
  |               |-- Lookaside cache
  |               |-- ZStreamSearchTool  (uses Jira API internally)
  |
  |-- stdio --> ymir-unprivileged (child process)
                  |-- Local filesystem, shell, specfile, git
                  |-- UpstreamSearchTool    --> UPSTREAM_SEARCH_API_URL
```
