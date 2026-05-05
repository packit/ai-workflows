# Running the Triage Skill with Mocked Data

This guide explains how to set up a local environment for executing the triage
skill against mocked JIRA data and pre-fix CentOS Stream repos — without
touching any production services.

---

## Prerequisites

- MCP tools installed (`ymir-common` + `ymir-tools`) as described in
  `skills_installation.md`
- The triage skill installed (see `skills_installation.md` for Claude Code /
  Cursor instructions)
- Access to the private test data repository:
  `git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git`
- A valid `rhel-config.json` (template in `templates/rhel-config.json`)

## 1. Clone the test data repository

```bash
git clone git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git /tmp/testing-jiras
```

The repository layout:

```
testing-jiras/
├── jiras/                  ← JIRA mock files (one JSON file per issue key)
│   ├── RHEL-112546
│   ├── RHEL-15216
│   ├── RHEL-29712
│   └── RHEL-61943
├── mock_data/              ← repo fixture configs (repos to clone at pre-fix state)
│   ├── RHEL-112546.json
│   ├── RHEL-15216.json
│   ├── RHEL-29712.json
│   └── RHEL-61943.json
└── README.md
```

### JIRA mock files (`jiras/`)

Each file is a JSON snapshot of a JIRA issue as returned by the REST API.
When `MOCK_JIRA=true`, the mock HTTP session reads these instead of calling
the real JIRA API.

### Repo fixture configs (`mock_data/`)

Each JSON file describes CentOS Stream repos to clone at a pre-fix state so
the agent cannot "cheat" by finding the already-applied backport:

```json
{
    "zstream_override": {"9": "rhel-9.2.z"},
    "repos": [
        {
            "package": "libtiff",
            "remote_url": "https://gitlab.com/redhat/centos-stream/rpms/libtiff",
            "pre_fix_ref": "<pre-fix-commit>",
            "branch": "c9s"
        }
    ]
}
```

- **`repos`** (required): list of repos to bare-clone and rewind. The
  `pre_fix_ref` is the commit hash to reset the branch to (before the fix
  was applied).
- **`zstream_override`** (optional): overrides what the agent considers the
  "current" z-stream for a given major version, so it follows the normal
  backport path instead of the restricted older-zstream path.

See `ymir/common/mock_repos.py` for full schema documentation.

## 2. Prepare `rhel-config.json`

Several tools (`map_version`, `check_cve_triage_eligibility`,
`normalize_fix_version`) load `rhel-config.json` from the current working
directory at runtime. Copy the template:

```bash
cp templates/rhel-config.json ./rhel-config.json
```

Make sure the unprivileged MCP gateway is started from the directory that
contains this file (or symlink it there).

## 3. Ensure writable JIRA mock files

The mock JIRA session writes back to the files (e.g. when adding comments or
updating fields), so they need to be writable:

```bash
chmod -R u+w /tmp/testing-jiras/jiras/
```

## 4. Environment variables reference

### Privileged gateway (`ymir-privileged`)

| Variable | Value | Purpose |
|---|---|---|
| `MOCK_JIRA` | `true` | Swaps the real HTTP client for a mock that reads/writes JSON files |
| `JIRA_MOCK_FILES` | `/tmp/testing-jiras/jiras` | Directory containing JIRA mock files |
| `JIRA_DRY_RUN` | `true` | Prevents any JIRA write operations (comments, fields, status) |

### Unprivileged gateway (`ymir-unprivileged`)

| Variable | Value | Purpose |
|---|---|---|
| `MOCK_REPOS_DIR` | `/tmp/testing-jiras/mock_data` | Directory with per-issue repo fixture JSON files. At runtime the repos are bare-cloned, rewound to the pre-fix commit, and git `insteadOf` rewriting redirects git operations to the local clone. |
| `MOCK_BLOCKED_URLS` | Comma-separated URL prefixes | Blocks `curl`/`wget` access to these URLs in `RunShellCommandTool`. Automatically populated when `MOCK_REPOS_DIR` is used, but can also be set explicitly. |
| `MOCK_ZSTREAMS` | JSON string, e.g. `{"9":"rhel-9.2.z"}` | Standalone z-stream override (applied globally). Per-issue overrides in fixture files take precedence. |

> **Note:** `MOCK_REPOS_DIR` automatically populates `MOCK_BLOCKED_URLS` at
> runtime via `_register_blocked_urls()`. However, if you want the URL
> blocking to be active from gateway startup (before any tool invocation),
> set `MOCK_BLOCKED_URLS` explicitly as well.

## 5. MCP configuration

### Claude Code (`~/.claude.json`)

```json
{
  "mcpServers": {
    "ymir-privileged": {
      "command": "ymir-privileged-gateway",
      "env": {
        "MCP_TRANSPORT": "stdio",
        "GITLAB_TOKEN": "<your-gitlab-token>",
        "JIRA_URL": "https://redhat.atlassian.net",
        "JIRA_EMAIL": "you@redhat.com",
        "JIRA_TOKEN": "your-jira-api-token",
        "KRB5CCNAME": "FILE:/tmp/krb5cc_1000",
        "MOCK_JIRA": "true",
        "JIRA_MOCK_FILES": "/tmp/testing-jiras/jiras",
        "JIRA_DRY_RUN": "true"
      }
    },
    "ymir-unprivileged": {
      "command": "ymir-unprivileged-gateway",
      "env": {
        "MCP_TRANSPORT": "stdio",
        "UPSTREAM_SEARCH_API_URL": "http://upstream-search.hosted.upshift.rdu2.redhat.com:80/v1",
        "MOCK_REPOS_DIR": "/tmp/testing-jiras/mock_data",
        "MOCK_BLOCKED_URLS": "<comma-separated remote_url values from the fixture>"
      }
    }
  }
}
```

Replace `MOCK_BLOCKED_URLS` with the `remote_url` values from the fixture
file for the issue you're testing. For example, for `RHEL-15216`:

```
"MOCK_BLOCKED_URLS": "https://gitlab.com/redhat/centos-stream/rpms/dnsmasq"
```

For project-scoped configuration, place the same `mcpServers` block in
`.claude/settings.json` inside your project directory instead.

### Cursor

Use the same env vars in your Cursor MCP configuration.

## 6. Run the skill

Start a session and invoke the triage skill with the desired issue key:

```
Use the triage skill with jira_issue=RHEL-15216 and dry_run=true
```

### What happens under the hood

1. The **privileged gateway** reads the JIRA mock file (e.g.
   `/tmp/testing-jiras/jiras/RHEL-15216`) instead of calling the real JIRA
   API.
2. The **unprivileged gateway** reads the repo fixture config (e.g.
   `/tmp/testing-jiras/mock_data/RHEL-15216.json`), bare-clones the
   CentOS Stream repo, rewinds its branch to the pre-fix commit, and sets
   up `GIT_CONFIG` `insteadOf` rewriting so all git operations against the
   remote URL are transparently redirected to the local clone.
3. If the fixture file contains a `zstream_override`, the z-stream mapping
   is overridden via the `current_z_streams_override` ContextVar.
4. If the agent tries to `curl` or `wget` a mocked repo URL,
   `RunShellCommandTool` blocks it with a `ToolError`.
5. `JIRA_DRY_RUN=true` prevents any writes to the mock JIRA files;
   `dry_run=true` (skill argument) skips posting the final comment.

## Available test scenarios

| Issue key | Package | Expected resolution | Notes |
|---|---|---|---|
| `RHEL-15216` | dnsmasq | backport | Standard backport from upstream gitweb |
| `RHEL-112546` | libtiff | backport | Z-stream override (`9` → `rhel-9.2.z`), multiple patches |
| `RHEL-61943` | dnsmasq | backport | Z-stream backport |
| `RHEL-29712` | bind | backport | Standard backport |

## Troubleshooting

### `rhel-config.json not found`

The unprivileged gateway loads `rhel-config.json` from its current working
directory. Make sure the file exists there:

```bash
cp templates/rhel-config.json ./rhel-config.json
```

### JIRA mock files are not writable

If you see write errors from the mock session, ensure permissions:

```bash
chmod -R u+w /tmp/testing-jiras/jiras/
```

### Agent uses `curl`/`wget` instead of `git` to access the repo

Set `MOCK_BLOCKED_URLS` to include the mocked `remote_url` values. The
`RunShellCommandTool` will raise a `ToolError` before the command executes,
guiding the agent to use git instead.
