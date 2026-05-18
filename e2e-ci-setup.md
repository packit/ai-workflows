# Triage Agent E2E CI Setup

## Abstract

The triage agent e2e tests exercise a real LLM against mock Jira fixtures and
git repos. Running these in the internal Red Hat Testing Farm (via Packit) is
the best fit for this project because:

1. **Log privacy** -- test output contains Jira issue details and agent
   reasoning about CVEs. On the internal ranch, only Red Hat employees can
   view logs and artifacts, preventing accidental exposure of sensitive data.

2. **Data stays within internal infrastructure** -- credentials, mock
   fixtures, and test results never leave the Red Hat network. There is no
   risk of secrets leaking through public CI logs or artifacts.

3. **No separate service to maintain** -- unlike a self-hosted Jenkins
   instance, Testing Farm is a managed service. Packit integrates it directly
   into the GitHub PR workflow with zero infrastructure overhead for the team.

> **Note:** This document exists to transfer knowledge about the CI setup and
> the rationale behind it. Once all the steps below are completed and the CI
> is operational, this file can be removed from the repository.

## Post-merge setup

This section describes the manual steps required to activate the Packit CI
e2e tests after the initial PR is merged.

## Prerequisites

### 1. Install the Packit GitHub App

If not already installed, add the [Packit GitHub App](https://github.com/apps/packit-as-a-service)
to the repository. This allows Packit to react to PR comments and push events.

### 2. Request approval for internal Testing Farm

The `.packit.yaml` uses `use_internal_tf: true` which requires explicit
approval from the Packit team. Open a request in the
[Packit tracker](https://github.com/packit/packit-service/issues) or reach
out on the `#packit` IRC/Matrix channel asking them to enable `use_internal_tf`
for this repository.

### 3. Create the CI secrets repository

Create a new **private** repository on `gitlab.cee.redhat.com` (e.g.
`jotnar-project/ci-secrets`) containing the following files at the root:

| File | Contents |
|------|----------|
| `beeai-agent.env` | `CHAT_MODEL`, API keys, BeeAI config (see `templates/beeai-agent.env`) |
| `mcp-gateway.env` | `GITLAB_TOKEN`, Jira credentials (see `templates/mcp-gateway.env`) |
| `rhel-config.json` | RHEL configuration used by the agent |
| `jotnar-vertex-dev.json` | Google Vertex AI service-account key (only if using `vertexai:` prefix) |

### 4. Create a GitLab access token

Create a personal access token (or deploy token) on `gitlab.cee.redhat.com`
with **read** access to both:

- `jotnar-project/ci-secrets`
- `jotnar-project/testing-jiras`

### 5. Encrypt the token for Testing Farm

Install the `testing-farm` CLI and encrypt the token value:

```bash
testing-farm encrypt \
  --git-url https://github.com/<org>/ai-workflows \
  --token-id ea2a89aa-6a78-40e0-906e-140f623c45b0 \
  "<your-gitlab-token>"
```

The `--token-id` is Packit Production's public ID for the RH-internal ranch.

### 6. Update `.testing-farm.yaml`

Replace the placeholder in `.testing-farm.yaml` with the encrypted value
from the previous step:

```yaml
version: 1

environments:
  secrets:
    GITLAB_CI_TOKEN: "<paste encrypted output here>"
```

Commit and push this change to the repository.

## Usage

Once the above steps are complete:

- **On pull requests**: A maintainer posts `/packit test` as a comment to
  trigger the e2e tests. Results appear as a GitHub check
  (`testing-farm:centos-stream-10-x86_64:triage-e2e`).

- **On push to main**: Tests run automatically after each merge, catching
  regressions even if the maintainer forgot to test the PR.

- **Viewing results**: Logs and artifacts are only visible on the private
  Red Hat Testing Farm ranch (requires Red Hat VPN/network access).
