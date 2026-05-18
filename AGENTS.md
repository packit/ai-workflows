# AGENTS.md - Development Guide for AI Agents

This guide is designed for AI agents working on the Ymir AI workflows project. It focuses on agent-specific patterns and gotchas. For general setup and deployment instructions, see [README-agents.md](README-agents.md) and [README.md](README.md).

## Before You Start

**Consult these first:**
- **[README-agents.md](README-agents.md)** — Full setup, running agents, environment variables, Jira mocking
- **[README.md](README.md)** — Project overview, development environment setup
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — Code merge policy

## Agent Architecture

Five agents process tasks through Redis queues (see [README-agents.md](README-agents.md)):
- **Triage Agent**: Analyzes Jira issues, determines resolution path (rebase vs backport)
- **Rebase Agent**: Updates packages to newer upstream versions
- **Backport Agent**: Applies specific fixes/patches to packages
- **Rebuild Agent**: Rebuilds packages in the build system
- **Supervisor Workflows**: Manage testing and release (see [README-supervisor.md](README-supervisor.md))

## Development Workflows

### Modifying Agent Logic

1. **Edit agent code** in `ymir/agents/`
2. **Run dry-run test**:
   ```bash
   make run-triage-agent-standalone JIRA_ISSUE=RHEL-12345 DRY_RUN=true MOCK_JIRA=true
   ```
3. **Use mock Jira** (from `git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git`) for consistent test data
4. **Verify with full pipeline**:
   ```bash
   make start DRY_RUN=true
   make trigger-pipeline JIRA_ISSUE=RHEL-12345
   ```
5. **Check agent logs**: Review output to ensure logic works as expected

### Modifying Tools (Git, Build System)

Tools in `ymir/tools/privileged/` require special care:

1. **Always write unit tests first** — especially for git operations
2. **Test against actual repos** (dist-git clones) when possible
3. **Run full test suite before submitting**:
   ```bash
   make check-in-container
   ```
4. **Key file**: `ymir/tools/privileged/distgit.py` — handles clone/checkout for dist-git


## Testing Patterns

### Unit Tests
```bash
# All tests in containers
make check-in-container

# Specific components in containers
make check-agents-in-container
make check-privileged-tools-in-container
make check-unprivileged-tools-in-container
```

> **Rootless podman**: if the build step fails with "cannot re-exec process to join the existing user namespace", skip the image build and run the container directly with `--privileged`:
> ```bash
> podman run --rm --privileged -v $(pwd):/src:z beeai-tests make -f Makefile.tests check-privileged-tools
> ```

### Manual Testing with Real Data Flow
1. Start full pipeline: `make start DRY_RUN=true`
2. Monitor traces: http://localhost:6006/ (Phoenix)
3. Monitor queues: http://localhost:8081/ (Redis)
4. Trigger: `make trigger-pipeline JIRA_ISSUE=RHEL-12345`
5. Review agent logs to see decision-making

## Common Issues & Gotchas

### Dist-Git Operations
- **Kerberos authentication**: Required for internal RHEL dist-git. Keytab in `.secrets/keytab`
- **SSH config**: Update `files/internal_dist-git_ssh.conf` User field with your Kerberos ID
- **Clone timeouts**: Large repos may timeout; adjust timeout in `ymir/tools/privileged/distgit.py` if needed

### CI Pipeline
- **COPY <<EOF variable trap**: Shell variables in COPY instructions don't expand. Use `RUN` with shell redirection instead
- **quay.io naming**: Images must push to `quay.io/jotnar/` namespace; verify Containerfile registry config

### OpenShift Deployment
- **RollingUpdate deadlock**: With `replicas=1`, RollingUpdate causes quota deadlock. **Must use `Recreate` strategy**
- **Memory quota exhaustion**: Total: 14Gi requests / 16Gi limits. Check `openshift/base/` resource definitions
- **Image pull failures**: Verify images exist in `quay.io/jotnar/` before deployment

For detailed deployment info: see [openshift/README.md](openshift/README.md)

## Key Files for Common Tasks

| Task | File |
|------|------|
| Add agent logic | `ymir/agents/*/main.py` or new agent file |
| Modify git operations | `ymir/tools/privileged/distgit.py` |
| Change Jira integration | `ymir/tools/jira/` + update [jira_data_flow.md](jira_data_flow.md) |
| Update OpenShift | `openshift/overlays/{dev,staging,prod}/` |
| Add skill | `agents_as_skills/{skill_name}/SKILL.md` |
| Change queue routing | [jira_label_workflow_routing.md](jira_label_workflow_routing.md) |

## Documentation to Understand Workflows

- **[jira_data_flow.md](jira_data_flow.md)** — Jira integration, issue tracking
- **[gitlab_distgit_data_flow.md](gitlab_distgit_data_flow.md)** — Dist-git clone/checkout operations
- **[jira_label_workflow_routing.md](jira_label_workflow_routing.md)** — Queue routing, label state machine, Jira workflow
- **[brew_konflux_data_flow.md](brew_konflux_data_flow.md)** — Build system integration
- **[ai_providers_data_flow.md](ai_providers_data_flow.md)** — Vertex AI integration
- **[monitoring.md](monitoring.md)** — Observability and performance review

## Code Changes Checklist

- [ ] Write tests first (especially for tools/git operations)
- [ ] Run `make check-in-container` — all tests pass
- [ ] Test with `DRY_RUN=true` — don't touch real Jira/git
- [ ] Use rebase merge (see [CONTRIBUTING.md](CONTRIBUTING.md))
- [ ] Don't modify `.env`, `.secrets/`, keytab files in PRs
- [ ] Update relevant documentation (README-agents.md, jira_data_flow.md, etc.)

## Useful Commands

```bash
# Environment
uv sync --extra test

# Run all tests in containers
make check-in-container

# Test single agent
make run-triage-agent-standalone JIRA_ISSUE=RHEL-12345 DRY_RUN=true MOCK_JIRA=true

# Run full pipeline with monitoring
make start DRY_RUN=true
make trigger-pipeline JIRA_ISSUE=RHEL-12345
```

See [README-agents.md](README-agents.md) for complete command reference.
