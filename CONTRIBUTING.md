# Contributing Guidelines

## Running E2E Tests Locally

E2E tests exercise a real LLM against mock Jira fixtures and git repos. They require test data
from the private `git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git` repository — you
only need to clone it once; the tests run entirely offline against the local copy.

### Setup

1. Clone the testing-jiras repository somewhere on your machine.

2. Add these variables to your local `.env` pointing at the testing-jiras checkout:
   ```
   JIRA_MOCK_FILES_HOST=/path/to/testing-jiras/jiras
   MOCK_REPOS_HOST=/path/to/testing-jiras/mock_data
   ```
   `JIRA_MOCK_FILES_HOST` mounts mock Jira issue data into the `mcp-gateway` container.
   `MOCK_REPOS_HOST` mounts mock git repo fixtures into the E2E test containers.

3. Add `GITLAB_TOKEN` to both `.env` and `.secrets/beeai-agent.env` with a GitLab
   personal access token that has read access to `gitlab.com/redhat`. It must be in
   `.env` so the compose `${GITLAB_TOKEN:-}` substitution resolves correctly (otherwise
   the empty `environment:` value overrides `env_file`). The mock repo setup clones
   from GitLab to create pre-fix-state bare repos; without it you'll be prompted for
   credentials.

### Running

```bash
make run-triage-agent-e2e-tests
make run-backport-agent-e2e-tests
```

Both targets hardcode `MOCK_JIRA=true` and `DRY_RUN=true`. The mock Jira files are writable,
so without `DRY_RUN=true` the agent would write comments back to them and corrupt the fixtures
on subsequent runs.

## Merging Policy

Prefer rebase-merging over creating a merge commit, unless preserving the branch's history is necessary.
