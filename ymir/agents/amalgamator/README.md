# Amalgamator adapter

Amalgamator is an independent backport writer: given an upstream repository, a
base commit, and the upstream fix, it produces a `candidate.patch` plus a
`dossier.md` explaining what it did. It does not commit, edit a spec, build an
SRPM, or open a merge request — it supplies a patch only.

This package lets Ymir use a patch from Amalgamator for the same issue the
backport agent works on, so the two writers can be compared on identical
inputs. The comparison is the point: neither writer is assumed to be better
until there are numbers from real issues.

## Ships in stages

1. **Adapter, translator, and measurement arm (this change).** Converts Ymir
   inputs into an Amalgamator case, runs it, reads the artifacts back, and runs
   Amalgamator over Ymir's own e2e fixtures. Default-off: nothing in the
   production pipeline changes, and the arm skips when no checkout is present.
2. **A `run_amalgamator` step.** Registers after `run_backport_agent` and calls
   `adapter.run_case()`, which is the unit the arm already exercises.
3. **A mode switch.** One setting, `AMALGAMATOR_MODE`, decides who runs and
   whose patch opens the merge request: `ymir` (agent only, the default),
   `amalgamator` (Amalgamator only), `both-ymir` (both run, the agent's patch
   opens the MR, Amalgamator's is attached for comparison) and
   `both-amalgamator` (both run, Amalgamator's patch opens the MR). The first
   comparison runs in `both-ymir`.

Stage 2 is deliberately not part of stage 1. Amalgamator runs take hours on
non-trivial drift, so whether it belongs on every issue — and therefore whether
the step is synchronous or asynchronous — depends on the stage 1 measurements.

## Pieces

| Module | Responsibility |
| --- | --- |
| `adapter.py` | Builds the case file, runs `python -m auto_bug_fix.run_bug_fix`, reads `summary.json`, `candidate.patch`, and `dossier.md`. Stdlib only, usable as a CLI. |
| `inputs.py` | Translates what Ymir already has into adapter arguments: package version from the dist-git spec, upstream repository and fix commit from the upstream patch URL, description from the ticket. |
| `tests/e2e/backport_agent/amalgamator_arm.py` | Per-issue glue for the e2e arm: prepares a clean upstream clone under its own work directory. |
| `tests/e2e/backport_agent/test_backport_amalgamator.py` | Runs Amalgamator over the same fixtures the agent suite uses and compares file overlap with each fixture's reference patch. |
| `common/upstream_tags.py` | Release-tag conventions shared with the agent's `find_base_commit` tool, so both writers choose the base commit by the same rules. |

## Input mapping

| Amalgamator field | Source in Ymir |
| --- | --- |
| `issue_id` | Jira issue key |
| `bug_description` | Ticket summary (the e2e arm reads the mock Jira files; the future step passes the triage summary) |
| `repository.source_path` | A pristine upstream clone, never the agent's working copy |
| `repository.workdir` | Per-run scratch directory; Amalgamator works in `<workdir>/<repo>` |
| `branches.target` | The pre-fix base commit — a SHA, not a branch name |
| `branches.source` | The upstream default branch; always written because Amalgamator would otherwise assume `origin/main` |
| `fix.commit` | Preferred: the commit named by the patch URL, or the merge commit of a merged pull/merge request |
| `fix.patch` | Fallback when the URL names no commit; must be a single-message patch |
| `build.command`, `build.test_command` | Empty by default. When set, a command runs at baseline and after the patch; when empty, only that the patch applies is checked. |
| `model` | Passed through from the runner; a profile id or a bare model name (see *Choosing the model*) |

Two details worth keeping in mind:

- A pull/merge request `.patch` endpoint returns one message per commit and
  Amalgamator rejects such mboxes. The merge commit carries the whole topic
  branch relative to its first parent, and Amalgamator cherry-picks it with
  `-m 1`, so merged request URLs are resolved to their merge commit. An open
  request has no merge commit yet and falls back to the patch.
- A `candidate.patch` on disk does not mean the run succeeded: Amalgamator also
  writes it when the pipeline stops, escalates, or only partially succeeds. The
  adapter calls a run successful only when it produced a patch, exited 0, and
  the target outcome is `success`.
- The `Version:` line of a spec is not always literal. Indirect versions
  (`Version: %{foo}`) and libexpat's older `%(echo %{unversion} | sed ...)`
  form are expanded; anything still holding an unknown macro is reported as
  unresolved instead of being passed on as a version.

## Choosing the model

The case file's `model` value is optional and is either a **profile id** from a
`models.yaml` registry or a **bare model name**:

```yaml
models:
  m1:
    provider: anthropic
    model: claude-sonnet-5-5
    api_key_file: /home/beeai/amalgamator-models/anthropic.key
default: m1
```

- A named profile carries the whole transport: `provider`, `model`, `api_base`,
  `api_key` or `api_key_file`, and `params`. Inline `api_base`/`api_key`/
  `model_params` in the case are ignored when a profile id is used. Registry
  values are not environment-interpolated; to use the provider's environment
  credentials, omit the key fields. An `api_key_file` path is resolved against
  the working directory (the case directory) after `~` expansion — inside the
  container, `~` is `/home/beeai`, so use an absolute path under the mounted
  registry such as `/home/beeai/amalgamator-models/anthropic.key`.
- A bare name (`claude-sonnet-5-5`) builds a synthetic profile and takes its
  credentials from the environment: `ANTHROPIC_API_KEY`, or, for an
  OpenAI-compatible model, `OPENAI_API_KEY` and `OPENAI_BASE_URL`. A gateway
  model is written litellm-style, for example
  `openai/rits/deepseek-ai/DeepSeek-V4.1-Flash-1017`.
- With no `model` at all, the registry's `default` profile is used.

The registry is found, not merged, in this order:

1. an explicit `--models` path — the adapter's `models_path`
   (`AMALGAMATOR_MODELS` in the e2e arm). An explicit path must exist: a bad
   one fails the run instead of falling back to discovery.
2. `models.yaml` in the directory of the case file.
3. `~/.config/amalgamator/models.yaml`.

A registry marked `strict: true` rejects a name that is not a profile id; a
non-strict registry warns and builds a synthetic profile.

One model serves every phase. Amalgamator can be told to use a different model
for Phase 4b with `advanced.phase_4b_model` in the case file; the adapter does
not write `advanced`, and with the build and test commands empty Phase 4b is
unreachable anyway. Add the key to `build_case_config` if a pipeline step ever
needs a separate Phase 4b model.

## Build, tests, and repair

`build.command` and `build.test_command` are left empty on purpose. Empty means
skipped, not a fallback: Amalgamator omits its baseline build, verification
passes without compiling anything, and its build-error and run-and-fix phases
never run. A `success` outcome therefore means the fix applied to the base tree
and matched the expected commit — not that it compiles.

Ymir's build stays the compile gate for either writer: `run_build` submits the
SRPM to Copr/Konflux, and a failure enters `fix_build_error`, which repairs and
rebuilds before an MR can open.

Handoff rules for the pipeline step:

- Check that the selected patch applies to the dist-git tree before the build.
  Amalgamator's apply guarantee is against the upstream clone, and a patch that
  applies there can still miss after the spec's `%prep`; keeping that separate
  from a compile failure is what makes the comparison meaningful.
- Give whichever writer won the same downstream. A failed build should re-enter
  `fix_build_error` rather than fail the issue, so both writers get the same
  repair budget.
- Record whether the patch that reached the MR is the writer's draft or a
  repaired version; otherwise the comparison credits the writer with the repair
  loop's work.

Real commands are possible and would make the pipeline stronger — a non-zero
exit starts Amalgamator's repair loop instead of only reporting a check — but
the command must build green at baseline, because the later build gate requires
exit 0 rather than comparing against the baseline. The Ymir image ships gcc,
make, cmake, meson and autotools but not per-package `-devel` packages, so a
command list is per-package work: `libexpat` and `dnsmasq` build as they are;
`curl`, `nginx`, `libtiff` and `memcached` need dependencies added.

## Running the measurement arm

The arm needs an Amalgamator checkout, a Python 3.12+ environment with its
dependencies, a model with credentials, and the e2e fixtures:

```bash
make run-backport-agent-amalgamator-e2e-tests \
    AMALGAMATOR_HOST_PATH=/path/to/amalgamator \
    AMALGAMATOR_VENV_HOST_PATH=/path/to/amalgamator/.venv \
    AMALGAMATOR_MODEL=<profile-or-model> \
    AMALGAMATOR_MODELS=/home/beeai/amalgamator-models/models.yaml \
    MOCK_REPOS_HOST=/path/to/testing-jiras/mock_data \
    JIRA_MOCK_FILES_HOST=/path/to/testing-jiras/jiras
```

Both the checkout and the environment are bind-mounted read-only into the
container, so the environment must be built against the container's Python, not
the host's. Building it with the e2e image itself is the reliable way:

```bash
podman run --rm -v "$PWD/../amalgamator:/src:z" beeai-agent:c10s \
    sh -c 'python3 -m venv /src/.venv && /src/.venv/bin/pip install \
        anthropic claude-agent-sdk colorama "litellm>=1.100.1,<2" pyyaml'
```

Those are the `dependencies` in Amalgamator's `pyproject.toml`; the project
itself does not need installing, because the adapter puts the checkout on
`PYTHONPATH` and runs `python -m auto_bug_fix.run_bug_fix`. The venv records
only paths inside the image (`/usr/bin/python3.12`), so the mount point the
adapter uses does not have to match where it was created. A venv built on macOS
or against a `uv`-managed interpreter fails the suite's executable check and
the arm skips.

`AMALGAMATOR_MODELS` is a path **inside the container**. The registry directory
is `.secrets` by default (`AMALGAMATOR_MODELS_HOST_PATH` overrides it), so a
profile file at `.secrets/models.yaml` is
`/home/beeai/amalgamator-models/models.yaml` in the container. Leave
`AMALGAMATOR_MODELS` unset when the profile is a bare model name whose key is
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` in `.secrets/beeai-agent.env`. The suite
runs as `beeai`, not as the host user, so the registry and any `api_key_file`
must be readable by it (`chmod a+r`); an unreadable registry fails the run.

`AMALGAMATOR_TIMEOUT` bounds each case in seconds (default 21600, six hours, so
a slow model still completes). Runtime is model-dependent — GLM-5.3 took 2h20m
to 4h07m per case — and `amalgamator_result.json` records the actual figure.
`BACKPORT_E2E_ONLY_ISSUES` selects cases by Jira key, so one issue can be
smoke-tested without running the suite.

`MOCK_REPOS_HOST` supplies the backport fixtures and `JIRA_MOCK_FILES_HOST` the
ticket summaries. The shared fixture setup clones the dist-git remotes named in
each fixture, some of which need `GITLAB_TOKEN`. Fixtures are skipped, not
failed, when their fix URL names neither a commit nor a repository (a gitweb
query or a vendor download-site diff); the arm also skips when no Amalgamator
checkout is mounted.

Results are written under `/tmp/backport_e2e_artifacts/amalgamator/<issue>/`:
`amalgamator_result.json` plus the paths to `candidate.patch` and `dossier.md`.
`AMALGAMATOR_E2E_INFORMATIONAL=true` records a run that did not succeed without
failing the suite; an adapter failure still fails. Set it to `false` once the
numbers are stable.

## Levels of verification

- Unit tests cover case construction, artifact discovery, tag patterns, and URL
  translation; they need no network, model, or build.
- The generated case files can be checked against Amalgamator's own
  `validate_config` without credentials.
- A full run invokes a model per issue and can take hours; it is a measured
  experiment, not a unit test, and its numbers are what stage 2 depends on.
