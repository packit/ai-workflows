# End-to-End (E2E) Tests

## Safety Requirements

**CRITICAL:** E2E tests use real issue keys (e.g., RHEL-174694) as test fixtures. To prevent accidental writes to production Jira, these tests **MUST** run with production writes disabled.

### Required Environment Variables

At least ONE of the following MUST be set:

- **`MOCK_JIRA=true`** (recommended) — Uses file-based mock Jira backend
- **`DRY_RUN=true`** — Skips all Jira API writes

The test suite will **fail immediately** if neither is set.

### Running E2E Tests Safely

#### Via Make (Recommended)

The Makefile automatically sets both safety vars:

```bash
# Triage agent E2E tests
make run-triage-agent-e2e-tests

# Backport agent E2E tests
make run-backport-agent-e2e-tests
```

#### Manual Execution

If running pytest directly, you MUST set the env vars:

```bash
MOCK_JIRA=true DRY_RUN=true pytest ymir/agents/tests/e2e/test_triage.py
```

#### In CI/CD

Testing Farm and GitHub Actions workflows are configured to set these automatically. Do NOT modify CI configs to remove these vars.

## Why This Matters

**Real Incident:** In June 2026, E2E tests were accidentally run against production Jira without `MOCK_JIRA` or `DRY_RUN` set. This caused test comments to be posted to real CVE issues (e.g., RHEL-174694), creating confusion about whether the production Ymir pipeline was re-processing already-closed issues.

The mystery was traced to E2E tests being invoked directly (bypassing the Makefile) while `.secrets/mcp-gateway.env` (containing production `JIRA_URL`) was loaded.

## Test Fixtures

E2E tests use real Jira issue keys that exist in production. When `MOCK_JIRA=true`, these issues are loaded from mock JSON files in:
- `ymir/tools/privileged/tests/data/` (fetched from `testing-jiras` repo)

The mock Jira backend reads/writes local files only — no Jira network calls are made.

### GitLab Authentication for Local E2E Runs

For local Compose E2E runs (including `make run-<agent>-agent-e2e-tests`), add a GitLab.com personal access token that can read the repositories referenced by the mock repo fixtures to the project `.env`:

```bash
GITLAB_TOKEN=<GitLab token>
```

The mock repo setup clones the repositories described by the mock repo fixtures. Without `GITLAB_TOKEN` in `.env`, Git prompts for credentials while preparing mock repositories. See [`mock_repos/README.md`](mock_repos/README.md) for mock repository fixture setup.

## Debugging Failed Safety Checks

If you see:

```
SAFETY CHECK FAILED: E2E tests MUST run with production Jira writes disabled.
```

**Do NOT disable the check.** Instead:

1. Set `MOCK_JIRA=true DRY_RUN=true` when invoking pytest
2. Use `make run-triage-agent-e2e-tests` instead of direct pytest
3. Check if your shell has stray env vars overriding the defaults
