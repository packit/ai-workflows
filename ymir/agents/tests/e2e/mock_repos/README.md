# Where are the mock data files?

This directory contains mock repo fixture files organized by agent type.
The fixtures come from `git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git`
if you have access.

Clone the repository and either:

- **Run via compose (recommended):** set `TESTING_JIRAS_DIR` in your local `.env` to point
  at the cloned repository root so compose mounts it into the container:
  ```
  TESTING_JIRAS_DIR=/path/to/testing-jiras
  MOCK_REPOS_DIR=/testing-jiras/mock_data/triage
  BACKPORT_MOCK_REPOS_DIR=/testing-jiras/mock_data/backport
  ```

- **Run directly on host:** symlink the `mock_data/` subdirectories here:
  ```bash
  ln -s /path/to/testing-jiras/mock_data/triage ymir/agents/tests/e2e/mock_repos/triage
  ln -s /path/to/testing-jiras/mock_data/backport ymir/agents/tests/e2e/mock_repos/backport
  ```

> **Note:** Symlinks only work when running tests on the host. Podman bind
> mounts do not follow symlinks pointing outside the mounted tree, so
> containerized e2e tests require the compose-based approach.

## Expected layout

```
mock_repos/
├── triage/                     # Triage agent e2e fixtures
│   ├── RHEL-112546.json
│   ├── RHEL-114607.json
│   ├── RHEL-15216.json
│   ├── RHEL-29712.json
│   └── RHEL-61943.json
└── backport/                   # Backport agent e2e fixtures
    ├── RHEL-15216.json
    └── reference_patches/
        └── RHEL-15216.patch
```

Each JSON file describes the repos to clone at a pre-fix state and an
optional z-stream override for a given Jira issue. See
`ymir/common/mock_repos.py` for the schema documentation.

Backport fixtures additionally contain `input`, `expected`, and optionally
reference patches for LLM judge evaluation.
