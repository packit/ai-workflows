# Ymir Skills -- Workstation Installation Guide

This guide covers two things:

1. **Installing skills** from the `agents_as_skills/` directory so your AI
   agent can use them.
2. **Installing the MCP tools** (`ymir-common` and `ymir-tools`) that the
   skills depend on, and configuring your agent to start both the
   **privileged** and **unprivileged** MCP gateways automatically.

The skills follow the [Agent Skills standard](https://agentskills.io/home) and
work with any [compatible client](https://agentskills.io/clients), including
[Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview),
[opencode](https://opencode.ai), and
[Cursor](https://www.cursor.com/).

---

## Installing skills

The skills in `agents_as_skills/` follow the [Agent Skills standard](https://agentskills.io/specification)
and work with any compatible client. Consult your client's documentation for
where to place skill directories:

- **Claude Code** — [Skills documentation](https://docs.anthropic.com/en/docs/claude-code/skills)
- **opencode** — [Skills documentation](https://opencode.ai/docs/skills/)
- **Other clients** — see the [Agent Skills client list](https://agentskills.io/clients)

The skills live in `agents_as_skills/` in this repository. You can either clone
the repo and point your client at that directory, or download individual
`SKILL.md` files directly:

```bash
REPO_URL="https://raw.githubusercontent.com/packit/ai-workflows/main/agents_as_skills"
SKILLS_DIR=~/.claude/skills  # adjust to your client's skills directory

for skill in backport rebase triage rebuild preliminary-testing issue-verification; do
  mkdir -p "$SKILLS_DIR/$skill"
  curl -fsSL "$REPO_URL/$skill/SKILL.md" -o "$SKILLS_DIR/$skill/SKILL.md"
done
```


---

## MCP Tools Installation

The skills listed above require the Ymir MCP tools to be installed and
running. The rest of this guide covers that setup.

## Overview

| Gateway | Module | Tools |
|---------|--------|-------|
| **Privileged** | `ymir.tools.privileged.gateway` | GitLab, Jira, Copr, Koji/Kerberos, lookaside cache, dist-git branching |
| **Unprivileged** | `ymir.tools.unprivileged.gateway` | Filesystem, shell, specfile, git operations, patches, upstream search, z-stream search |

Both gateways run as child processes (via `stdio` MCP transport) managed by
your agent client and are available simultaneously throughout the session.

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

Set the working directory in the MCP server configuration (see below) to the
directory that contains this file.

## 3. Environment variables

### Privileged gateway

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_TRANSPORT` | **Yes** | Set to `stdio` so the agent can communicate with the process. |
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
| `MCP_TRANSPORT` | **Yes** | Set to `stdio` so the agent can communicate with the process. |
| `UPSTREAM_SEARCH_API_URL` | **Yes** | Base URL of the upstream search service (provides `/find_repository` and `/find_commit` endpoints). |
| `MCP_GATEWAY_URL` | No | SSE URL of a running privileged gateway. See the note on cross-gateway calls below. |
| `DEBUG_FILE` | No | Path to a log file. When set, gateway logs are written to this file in addition to stderr. Useful for debugging local installations. |
| `REQUESTS_CA_BUNDLE` | No | Path to a Certificate Authority (CA) bundle, if additional or custom ones are required for workflow services. |

## 4. Configure your agent client

### Claude Code

Add both servers using `claude mcp add`:

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

Or edit `~/.claude.json` directly, adding to the top-level `mcpServers` object
(replace `<your-home>` with the absolute path — `~` is not expanded in JSON):

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
        "KRB5CCNAME": "FILE:/tmp/krb5cc_1000"
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

### opencode

Add both servers to `~/.config/opencode/opencode.json` under the `mcp` key
(replace `<your-home>` with the absolute path):

```json
{
  "mcp": {
    "ymir-privileged": {
      "type": "local",
      "command": ["<your-home>/.local/share/ymir-venv/bin/ymir-privileged-gateway"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "GITLAB_TOKEN": "<your-gitlab-token>",
        "JIRA_URL": "https://redhat.atlassian.net",
        "JIRA_EMAIL": "you@redhat.com",
        "JIRA_TOKEN": "your-jira-api-token",
        "KRB5CCNAME": "FILE:/tmp/krb5cc_1000"
      }
    },
    "ymir-unprivileged": {
      "type": "local",
      "command": ["<your-home>/.local/share/ymir-venv/bin/ymir-unprivileged-gateway"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "UPSTREAM_SEARCH_API_URL": "http://upstream-search.hosted.upshift.rdu2.redhat.com:80/v1"
      }
    }
  }
}
```

Restart opencode after editing the config.

### Note: Kerberos keytab

If you use a keytab for automated Kerberos authentication, add `KEYTAB_FILE`
to the privileged server's `env`:

```json
"KEYTAB_FILE": "/path/to/your.keytab"
```

Without a keytab, you must run `kinit` manually before launching your agent
so that a valid ticket exists in the cache pointed to by `KRB5CCNAME`.

If you are using `KEYRING` credential cache (used by Kerberos by default),
you do not need to set `KRB5CCNAME`. All you need is a successful `kinit`.

## 5. Verify the setup

After starting your agent, confirm both MCP servers are connected. In Claude
Code run `/mcp`; in opencode the server list appears at startup. You should
see tools from both `ymir-privileged` and `ymir-unprivileged`.

## Network topology

```
Agent client
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
