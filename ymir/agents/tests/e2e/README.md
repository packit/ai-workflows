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

To skip specific backport issues, pass a comma-separated list:

```bash
make run-backport-agent-e2e-tests BACKPORT_E2E_EXCLUDE_ISSUES=RHEL-218065,RHEL-112546
```

The variable can also be set in the shell environment, including for direct
Compose runs. It defaults to empty, so no issues are excluded by this option
unless explicitly listed.

#### Amalgamator A/B arm

A second backport suite runs **Amalgamator** on the same fixtures and compares
its patch with the fixture's reference patch. It does not run the Ymir agent,
so it can run on its own or beside the agent suite:

```bash
make run-backport-agent-amalgamator-e2e-tests \
    AMALGAMATOR_HOST_PATH=/path/to/amalgamator \
    AMALGAMATOR_VENV_HOST_PATH=/path/to/amalgamator/.venv \
    MOCK_REPOS_HOST=/path/to/testing-jiras/mock_data \
    JIRA_MOCK_FILES_HOST=/path/to/testing-jiras/jiras
```

The public tree ships the fixture layout, not the fixtures: point
`MOCK_REPOS_HOST` at the `mock_data` directory and `JIRA_MOCK_FILES_HOST` at the
`jiras` directory of the shared testing data repository.

A case can take hours, so to smoke-test the arm on one issue, pass
`BACKPORT_E2E_ONLY_ISSUES`; every other fixture is skipped (and its dist-git
repos are not cloned):

```bash
make run-backport-agent-amalgamator-e2e-tests \
    ... BACKPORT_E2E_ONLY_ISSUES=RHEL-114643
```

It needs a checkout of Amalgamator and a Python 3.12+ environment with its
dependencies installed. The suite skips when the checkout is absent, when
`AMALGAMATOR_PYTHON` does not point at an executable, or when no interpreter is
configured and the running Python is older than 3.12. Missing dependencies are
not detected up front: the run fails, and is recorded as xfail while
`AMALGAMATOR_E2E_INFORMATIONAL` is on.

Both are mounted read-only into the container, so the environment has to be
built against the container's Python. Build it with the e2e image:

```bash
podman run --rm -v "$PWD/../amalgamator:/src:z" beeai-agent:c10s \
    sh -c 'python3 -m venv /src/.venv && /src/.venv/bin/pip install \
        anthropic claude-agent-sdk colorama "litellm>=1.100.1,<2" pyyaml'
```

Those are Amalgamator's runtime dependencies from its `pyproject.toml`; the
checkout itself goes on `PYTHONPATH`, so it is not installed. A venv built on
macOS or against a `uv`-managed interpreter does not run in the container: its
`python` points at a host path, the executable check fails, and the suite skips.

Its inputs come from the same fixture files: the package version is read from
the dist-git spec at the fixture's pre-fix commit, the upstream fix from
`input.upstream_patches`, and the description from the mock Jira data, with a
fallback sentence when the ticket file is absent.

Fixtures whose fix URL is a merged pull/merge request are resolved to the merge
commit through the forge API, because a multi-commit `.patch` is a multi-message
mbox that Amalgamator rejects; set `GITHUB_TOKEN`/`GITLAB_TOKEN` to avoid
anonymous rate limits. An unmerged request falls back to the downloaded patch.
`GITLAB_TOKEN` is also what lets the shared fixture setup clone the
`gitlab.com/redhat/rhel/rpms/*` entries of a fixture. A fixture is skipped when
its fix URL names neither a commit nor a repository (for example a gitweb query
or a vendor download-site diff).

##### Choosing the model

`AMALGAMATOR_MODEL` is either a profile id from a `models.yaml` registry or a
bare model name such as `claude-sonnet-5-5`:

- A **profile id** carries the whole transport — `provider`, `model`,
  `api_base`, `api_key` or `api_key_file`, and `params`. Registry values are not
  environment-interpolated, and an `api_key_file` path is resolved against the
  case directory after `~` expansion (`/home/beeai` in the container), so point
  it at an absolute path under the mounted registry.
- A **bare name** builds a profile from the name alone. It takes its key from
  `ANTHROPIC_API_KEY`, or, for an OpenAI-compatible model, from `OPENAI_API_KEY`
  **and** `OPENAI_BASE_URL` — a gateway model is written litellm-style, for
  example `openai/rits/deepseek-ai/DeepSeek-V4.1-Flash-1017`. Both variables must
  be in `.secrets/beeai-agent.env` (the service does not otherwise forward them),
  or the model moved into a profile that carries `api_base` and `api_key`.
- Leaving `AMALGAMATOR_MODEL` unset uses the registry's `default` profile.

`AMALGAMATOR_MODELS` is a path **inside the container**. The registry directory
is mounted from `AMALGAMATOR_MODELS_HOST_PATH`, `.secrets` by default, at
`/home/beeai/amalgamator-models`, so a registry at `.secrets/models.yaml` is:

```bash
AMALGAMATOR_MODELS=/home/beeai/amalgamator-models/models.yaml
```

A host path has no effect. Amalgamator looks for the registry in the explicit
path first, then `models.yaml` beside the case file, then
`~/.config/amalgamator/models.yaml`; registries are not merged, and an explicit
path that does not exist fails the run instead of falling back. A registry
marked `strict: true` rejects a name that is not a profile id.

The suite runs as the container's `beeai` user, which is not the owner of the
host `.secrets` tree, so the registry and any `api_key_file` must be readable by
it (`chmod a+r`). An unreadable registry fails the run; an unreadable key file
only warns and leaves authentication to fail later.

One model serves every phase; Amalgamator's separate Phase 4b model
(`advanced.phase_4b_model`) is not written by the arm, and Phase 4b does not run
while the build and test commands are empty.

Runs are recorded under
`/tmp/backport_e2e_artifacts/amalgamator/<issue>/`, including
`amalgamator_result.json` and the paths to `candidate.patch` and `dossier.md`.
That JSON also carries `duration_s`, `amalgamator_duration_s` and `cost_usd` —
the wall-clock and cost numbers to weigh against the agent's.

`AMALGAMATOR_TIMEOUT` bounds each case, in seconds (default 21600, six hours).
Runtime is model-dependent: GLM-5.3 took 2h20m to 4h07m per case, and a faster
model finishes sooner. The default is headroom for a slow model, not an
expected duration — a shorter value kills a working run and records it as an
adapter error.

`AMALGAMATOR_E2E_INFORMATIONAL=true` (the Makefile default) records results
without failing when Amalgamator runs but does not succeed; an adapter failure
still fails. Set it to `false` once the numbers are stable — an empty value
still means `true`.

#### Manual Execution

If running pytest directly, you MUST set the env vars:

```bash
MOCK_JIRA=true DRY_RUN=true pytest ymir/agents/tests/e2e/test_triage.py
```

For the backport and Amalgamator suites, point `BACKPORT_MOCK_REPOS_DIR` at the
fixtures themselves — `<testing-jiras>/mock_data/backport`, not `mock_data` —
because Compose maps `MOCK_REPOS_HOST` to `/home/beeai/mock_repos` and appends
`backport`.

A host run of the Amalgamator arm needs Ymir's dependencies installed and four
more variables, all of which Compose normally supplies:

```bash
MOCK_JIRA=true DRY_RUN=true \
BACKPORT_MOCK_REPOS_DIR=<testing-jiras>/mock_data/backport \
JIRA_MOCK_FILES=<testing-jiras>/jiras \
GIT_REPO_BASEPATH=$TMPDIR/e2e-git-repos \
AMALGAMATOR_ROOT=/path/to/amalgamator \
AMALGAMATOR_PYTHON=/path/to/amalgamator/.venv/bin/python \
AMALGAMATOR_MODEL=<profile-or-model> \
BACKPORT_E2E_ONLY_ISSUES=RHEL-114643 \
pytest ymir/agents/tests/e2e/backport_agent/test_backport_amalgamator.py -v
```

`GIT_REPO_BASEPATH` must be writable (it defaults to `/git-repos`, which the
suite creates and deletes), and `JIRA_MOCK_FILES` supplies the ticket summaries.
An OpenAI-compatible model also needs `OPENAI_API_KEY` and `OPENAI_BASE_URL`
exported in the same shell.

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
