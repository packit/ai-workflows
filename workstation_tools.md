# Ymir MCP Tools -- Workstation Installation Guide

This guide covers installing the `ymir-common` and `ymir-tools` packages on a
local workstation and configuring Claude Code to start both the **privileged**
and **unprivileged** MCP gateways automatically.

## Overview

| Gateway | Module | Tools |
|---------|--------|-------|
| **Privileged** | `ymir_tools.privileged.gateway` | GitLab, Jira, Copr, Koji/Kerberos, lookaside cache, dist-git branching |
| **Unprivileged** | `ymir_tools.unprivileged.gateway` | Filesystem, shell, specfile, git operations, patches, upstream search, z-stream search |

Claude Code starts both servers in `stdio` mode and communicates with them
directly. Both run as child processes managed by Claude Code and are available
simultaneously throughout the session.

## Prerequisites

- Python >= 3.13
- Kerberos client tools (`kinit`, `klist`) -- required for Koji and dist-git operations
- System RPM bindings (`rpm` Python package)
- A valid `rhel-config.json` file (template provided in `templates/rhel-config.json`)

## 1. Install packages

Install `ymir-common` first because `ymir-tools` depends on it and the package
is not published on PyPI.

```bash
pip install "git+https://github.com/username/repository.git#subdirectory=ymir_common"
pip install "git+https://github.com/username/repository.git#subdirectory=ymir_tools"
```

After installation, two console scripts are available:

```
ymir-privileged-gateway
ymir-unprivileged-gateway
```

## 2. Prepare `rhel-config.json`

Several tools (`BuildPackageTool`, `CheckCveTriageEligibilityTool`,
`VersionMapperTool`) load `rhel-config.json` from the current working
directory at runtime. Copy the template and fill in the real values:

```bash
cp templates/rhel-config.json ~/rhel-config.json
# Edit ~/rhel-config.json with actual RHEL stream data
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

### Unprivileged gateway

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_TRANSPORT` | **Yes** | Set to `stdio` so Claude Code can communicate with the process. |
| `UPSTREAM_SEARCH_API_URL` | **Yes** | Base URL of the upstream search service (provides `/find_repository` and `/find_commit` endpoints). |
| `MCP_GATEWAY_URL` | No | SSE URL of a running privileged gateway. See the note on cross-gateway calls below. |

## 4. Configure Claude Code

Add both servers to your Claude Code MCP configuration. You can do this
with the CLI or by editing the settings file directly.

### Option A -- Using `claude mcp add`

```bash
claude mcp add ymir-privileged \
  --command ymir-privileged-gateway \
  --env MCP_TRANSPORT=stdio \
  --env GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx \
  --env JIRA_URL=https://issues.redhat.com/ \
  --env JIRA_EMAIL=you@redhat.com \
  --env JIRA_TOKEN=your-jira-api-token \
  --env KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u)

claude mcp add ymir-unprivileged \
  --command ymir-unprivileged-gateway \
  --env MCP_TRANSPORT=stdio \
  --env UPSTREAM_SEARCH_API_URL=http://your-upstream-search-service:port
```

### Option B -- Editing `~/.claude.json`

Add the following to the top-level `mcpServers` object:

```json
{
  "mcpServers": {
    "ymir-privileged": {
      "command": "ymir-privileged-gateway",
      "env": {
        "MCP_TRANSPORT": "stdio",
        "GITLAB_TOKEN": "glpat-xxxxxxxxxxxxxxxxxxxx",
        "JIRA_URL": "https://issues.redhat.com/",
        "JIRA_EMAIL": "you@redhat.com",
        "JIRA_TOKEN": "your-jira-api-token",
        "KRB5CCNAME": "FILE:/tmp/krb5cc_1000"
      }
    },
    "ymir-unprivileged": {
      "command": "ymir-unprivileged-gateway",
      "env": {
        "MCP_TRANSPORT": "stdio",
        "UPSTREAM_SEARCH_API_URL": "http://your-upstream-search-service:port"
      }
    }
  }
}
```

For project-scoped configuration, place the same `mcpServers` block in
`.claude/settings.json` inside your project directory instead.

### Optional: Kerberos keytab

If you use a keytab for automated Kerberos authentication, add `KEYTAB_FILE`
to the privileged server's `env`:

```json
"KEYTAB_FILE": "/path/to/your.keytab"
```

Without a keytab, you must run `kinit` manually before launching Claude Code
so that a valid ticket exists in the cache pointed to by `KRB5CCNAME`.

## 5. Verify the setup

Launch Claude Code and confirm both servers are running:

```
/mcp
```

This lists all connected MCP servers and their available tools. You should see
tools from both `ymir-privileged` and `ymir-unprivileged` servers.

## Note on cross-gateway calls

`ZStreamSearchTool` (unprivileged) contains internal logic that calls the
privileged tool `search_jira_issues` via the `MCP_GATEWAY_URL` SSE endpoint.
In the stdio setup described above, the privileged gateway is not exposed as
an HTTP server, so this internal cross-call is not available.

This has **no practical impact** when using Claude Code: because the model has
direct access to all tools from both servers simultaneously, it can call
`search_jira_issues` directly instead of relying on the embedded cross-gateway
delegation.

If you need the fully self-contained tool behavior (e.g. for automated
pipelines), run the privileged gateway separately as an SSE server and point
`MCP_GATEWAY_URL` at it:

```bash
# Terminal: start privileged gateway as SSE server
SSE_PORT=8001 ymir-privileged-gateway
```

Then in Claude Code, configure the privileged server as an SSE endpoint and
add `MCP_GATEWAY_URL` to the unprivileged server:

```json
{
  "mcpServers": {
    "ymir-privileged": {
      "type": "sse",
      "url": "http://localhost:8001/sse"
    },
    "ymir-unprivileged": {
      "command": "ymir-unprivileged-gateway",
      "env": {
        "MCP_TRANSPORT": "stdio",
        "UPSTREAM_SEARCH_API_URL": "http://your-upstream-search-service:port",
        "MCP_GATEWAY_URL": "http://localhost:8001/sse"
      }
    }
  }
}
```

In this variant the privileged gateway must be started manually before
launching Claude Code.

## Network topology

```
Claude Code
  |
  |-- stdio --> ymir-privileged (child process)
  |               |-- GitLab API        (GITLAB_TOKEN)
  |               |-- Jira API          (JIRA_EMAIL + JIRA_TOKEN)
  |               |-- Copr / Koji       (Kerberos)
  |               |-- Lookaside cache
  |
  |-- stdio --> ymir-unprivileged (child process)
                  |-- Local filesystem, shell, specfile, git
                  |-- UpstreamSearchTool    --> UPSTREAM_SEARCH_API_URL
                  |-- ZStreamSearchTool     --> MCP_GATEWAY_URL (optional, see above)
```
