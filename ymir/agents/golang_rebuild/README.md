# Golang CVE Rebuild Agent

Automates rebuilding RHEL 9.x and 10.x z-stream components affected by Golang CVE fixes. Integrates with ai-workflows infrastructure using GitLab MR workflow for all submissions.

## How It Works

```
Engineer adds comment to Jira ticket with build instructions (optional)
    |
Engineer applies "golang-rebuild-queue" label to trigger the agent
    |
Agent reads comment --> parses side-tag, commit hash, extra jiras, custom message
    |
Agent forks dist-git repo on GitLab (via MCP gateway)
    |
Agent bumps spec file: release version + changelog entry
    |
If commit hash provided: updates %global commit0, spectool -g, rhpkg new-sources
    |
Agent triggers scratch build (rhpkg scratch-build --srpm)
    |
Agent posts scratch build result to Jira --> STOPS
    |
Engineer reviews scratch build, adds "golang-rebuild-approved" label
    |
Agent commits, pushes to fork, opens GitLab MR for review
    |
Official build happens when MR is merged (via GitLab pipeline)
```

## Prerequisites

### System Tools

```bash
# Verify these are installed
rhpkg --version
brew --version
spectool --version
kinit -V
git --version
```

### Authentication

**Kerberos** (for rhpkg/brew):
```bash
kinit <username>@REDHAT.COM
klist  # verify ticket
```

**Jira API** (for reading tickets and posting comments):
```bash
# Create ~/.rh-jira-mcp.env with:
JIRA_URL=https://redhat.atlassian.net
JIRA_EMAIL=your.email@redhat.com
JIRA_API_TOKEN=<your-api-token>

# Load before running:
source ~/.rh-jira-mcp.env
export JIRA_USERNAME="$JIRA_EMAIL"
export JIRA_PASSWORD="$JIRA_API_TOKEN"
```

To get a Jira API token: https://id.atlassian.com/manage-profile/security/api-tokens

### MCP Gateway

The agent uses ai-workflows's MCP gateway for GitLab operations (fork, push, open MR). Set:
```bash
export MCP_GATEWAY_URL=http://mcp-gateway:8000/sse
```

### Python Dependencies

```bash
pip install jira pyyaml pydantic aiofiles pytest pytest-asyncio
```

## Setup

1. Clone ai-workflows and ensure the `ymir/agents/golang_rebuild/` directory is present
2. Copy and customize config:
   ```bash
   # Edit config.yaml to update allowed components and RHEL versions
   vi ymir/agents/golang_rebuild/config.yaml
   ```
3. Set environment variables:
   ```bash
   source ~/.rh-jira-mcp.env
   export JIRA_USERNAME="$JIRA_EMAIL"
   export JIRA_PASSWORD="$JIRA_API_TOKEN"
   export MCP_GATEWAY_URL=http://mcp-gateway:8000/sse
   export GOLANG_REBUILD_CONFIG=/path/to/config.yaml  # optional, auto-detected
   ```

## Usage

### For Engineers (Jira-based workflow)

#### Simple rebuild (no special instructions)

1. Find the component Jira ticket (e.g., RHEL-149580 for buildah)
2. Verify the parent Golang CVE ticket status is "Integration", "Release Pending", or "Done"
3. Apply label: **`golang-rebuild-queue`**
4. Agent will:
   - Auto-detect CVE IDs and RHEL version from ticket
   - Bump spec release and add changelog
   - Trigger scratch build
   - Post result to Jira
5. Review the scratch build result
6. If OK, apply label: **`golang-rebuild-approved`**
7. Agent opens a GitLab MR for final review and merge

#### Rebuild with side-tag (custom golang version)

When the buildroot has an older golang and you need a newer version:

1. Add a comment to the component ticket **before** applying the label:
   ```
   side-tag: rhel-9.4.0-z-gotoolset-stack-gate
   release: rhel-9.4.0
   ```
2. Apply label: **`golang-rebuild-queue`**
3. Agent uses the side-tag for scratch build

#### Rebuild with new commit hash

When sources need updating (new upstream commit):

1. Add a comment:
   ```
   commit: abc123def456789
   ```
2. Apply label: **`golang-rebuild-queue`**
3. Agent updates `%global commit0`, runs `spectool -g`, `rhpkg new-sources`

#### Full example comment (all options)

```
side-tag: rhel-9.4.0-z-gotoolset-stack-gate
release: rhel-9.4.0
commit: abc123def456789
jiras: RHEL-158645 RHEL-147034 RHEL-146820
message: Rebuilding with golang 1.25.8 for critical security fix
```

All fields are optional. If no comment is found, agent uses defaults.

### Comment Fields Reference

| Field | Description | Example |
|-------|-------------|---------|
| `side-tag` | Brew side-tag target (overrides default) | `rhel-9.4.0-z-gotoolset-stack-gate` |
| `release` | `--release` flag for rhpkg (required with side-tag) | `rhel-9.4.0` |
| `commit` | New commit hash for `%global commit0` | `abc123def456789` |
| `jiras` | Additional Jira IDs for changelog/commit | `RHEL-158645 RHEL-147034` |
| `message` | Custom changelog/commit message | `Rebuilding with golang 1.25.8 for security fix` |

### Jira Labels Reference

| Label | Purpose | Applied by |
|-------|---------|-----------|
| `golang-rebuild-queue` | Triggers the agent to process this ticket | Engineer |
| `golang-rebuild-approved` | Approves official build after scratch succeeds | Engineer |
| `jotnar_golang_rebuild_in_progress` | Agent is currently processing | Agent |
| `jotnar_golang_rebuild_completed` | Rebuild completed successfully | Agent |
| `jotnar_golang_rebuild_failed` | Rebuild failed | Agent |
| `jotnar_golang_rebuild_errored` | Unexpected error occurred | Agent |

### Direct Mode (environment variables)

```bash
export GOLANG_TICKET=RHEL-158645
export DRY_RUN=true
export MCP_GATEWAY_URL=http://mcp-gateway:8000/sse
python -m ymir.agents.golang_rebuild
```

### Queue Mode (Redis, for deployment)

```bash
export REDIS_URL=redis://valkey:6379/0
export MCP_GATEWAY_URL=http://mcp-gateway:8000/sse
export CONTAINER_VERSION=c9s
python ymir/agents/golang_rebuild/workflow.py
```

## What the Agent Produces

### Changelog Entry

```
* Mon May 05 2026 Golang Rebuild Agent <jotnar@redhat.com> - 2:1.33.13-3.1
- Rebuilding with golang 1.25.8 to fix net/http vulnerability
- Fixes: CVE-2025-12345 CVE-2025-67890
- Resolves: RHEL-149580 RHEL-158645 RHEL-147034
```

### Commit Message

```
Rebuilding with golang 1.25.8 to fix net/http vulnerability
Fixes: CVE-2025-12345 CVE-2025-67890
Resolves: RHEL-149580 RHEL-158645 RHEL-147034

Signed-off-by: Golang Rebuild Agent <jotnar@redhat.com>
```

### GitLab MR

Title: `Rebuild buildah for golang CVE fix`

Description includes scratch build NVR, Brew link, CVE list, and resolved Jira tickets.

## Configuration

Edit `config.yaml` to customize:

- **RHEL versions**: Add/remove z-stream versions with branch and build target
- **Component filter**: Control which components are processed
- **Brew settings**: Adjust polling interval and timeout for scratch builds

See `config.yaml` for inline documentation.

## File Structure

```
ymir/agents/golang_rebuild/
    __init__.py           # Package init
    __main__.py           # Entry point (python -m ymir.agents.golang_rebuild)
    workflow.py           # Main orchestrator (async, queue + direct mode)
    comment_parser.py     # Parses Jira comments for build instructions
    jira_queries.py       # Read-only Jira queries (CVE discovery)
    brew_client.py        # Async Brew/rhpkg scratch builds
    git_client.py         # Async git/rhpkg operations
    specfile.py           # RPM spec file parsing and modification
    models.py             # Pydantic data models
    constants.py          # Agent identity, component list, templates
    utils.py              # Helpers (CVE extraction, config loading)
    config.yaml           # Configuration file
    README.md             # This file
    tests/                # Unit tests (63 tests)
```

## Supported RHEL Versions

| Version | Branch | Build Target | Status |
|---------|--------|-------------|--------|
| RHEL 9.4.z | rhel-9.4.0 | rhel-9.4.0-candidate | Supported |
| RHEL 9.6.z | rhel-9.6.0 | rhel-9.6.0-candidate | Supported |
| RHEL 9.7.z | rhel-9.7.0 | rhel-9.7.0-candidate | Supported |
| RHEL 10.1.z | c10s | c10s-candidate | Supported |
| RHEL 8.x | - | - | Not supported |

## Running Tests

```bash
cd ai-workflows
PYTHONPATH=$(pwd) python -m pytest ymir/agents/golang_rebuild/tests/ -v
```

## Troubleshooting

### "Jira credentials not found"
```bash
source ~/.rh-jira-mcp.env
export JIRA_USERNAME="$JIRA_EMAIL"
export JIRA_PASSWORD="$JIRA_API_TOKEN"
```

### "No module named 'tasks'"
The `tasks` module is part of ai-workflows agents. Ensure PYTHONPATH includes both the repo root and agents directory:
```bash
export PYTHONPATH=/path/to/ai-workflows:/path/to/ai-workflows/agents
```

### Scratch build times out
Increase `max_wait_time` in `config.yaml` under `brew` section (default: 7200 seconds / 2 hours).

### "Branch not found"
The RHEL version in the ticket may not have a corresponding branch yet. Check that the branch exists in the dist-git repo.

### Kerberos expired
```bash
kinit <username>@REDHAT.COM
```

## Contact

- Jotnar team: jotnar@redhat.com
- Slack: #forum-jotnar-package-automation
