# Contributing Guidelines

## Running E2E Tests Locally

E2E tests exercise a real LLM against mock Jira fixtures and git repos. They require test data
from the private `git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git` repository — you
only need to clone it once; the tests run entirely offline against the local copy.

### Setup

1. Clone the testing-jiras repository somewhere on your machine.

2. Add `JIRA_MOCK_FILES_HOST` to your local `.env` pointing at its `jiras/` directory:
   ```
   JIRA_MOCK_FILES_HOST=/path/to/testing-jiras/jiras
   ```
   This makes compose mount that directory into the `mcp-gateway` container at the path
   where mock Jira files are expected.

3. Symlink the mock repo fixtures for git operations:
   ```bash
   ln -s /path/to/testing-jiras/mock_data/triage ymir/agents/tests/e2e/mock_repos/triage
   ln -s /path/to/testing-jiras/mock_data/backport ymir/agents/tests/e2e/mock_repos/backport
   ```

### Running

```bash
make run-triage-agent-e2e-tests DRY_RUN=true
make run-backport-agent-e2e-tests
```

## Merging Policy

Prefer rebase-merging over creating a merge commit, unless preserving the branch's history is necessary.
