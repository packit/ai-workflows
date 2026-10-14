# Where are the mock data files?

This directory contains mock repo fixture files organized by agent type.
The fixtures come from `git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git`
if you have access.

Clone the repository and set `MOCK_REPOS_HOST` in your `.env`:

```
MOCK_REPOS_HOST=/path/to/testing-jiras/mock_data
```

It is recommended to run the E2E tests via compose.

You can also symlink the `mock_data/` subdirectories here for running tests on the host:
```bash
ln -s /path/to/testing-jiras/mock_data/triage ymir/agents/tests/e2e/mock_repos/triage
ln -s /path/to/testing-jiras/mock_data/backport ymir/agents/tests/e2e/mock_repos/backport
```

> **Note:** Symlinks only work when running tests on the host. Podman bind
> mounts do not follow symlinks pointing outside the mounted tree, so
> containerized e2e tests require the compose-based approach.

This mounts the fixture data into E2E test containers at `/home/beeai/mock_repos/`.
The compose services set `MOCK_REPOS_DIR` and `BACKPORT_MOCK_REPOS_DIR` to point
at the `triage/` and `backport/` subdirectories respectively.

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
optional z-stream or RHEL configuration override for a given Jira issue. See
`ymir/common/mock_repos.py` for the schema documentation.

Backport fixtures additionally contain `input`, `expected`, and optionally
reference patches for LLM judge evaluation.

## Per-test RHEL configuration

> **Note:** To keep an E2E fixture reproducible, include the release-stream
> values from the `rhel-config.json` snapshot that was in effect when its
> expected result was created. This prevents later release-stream changes
> from silently changing the test's behavior.

To set RHEL configuration values for an E2E test, add an optional
`rhel_config_override` at the top level of its fixture in
`mock_data/triage/`, `mock_data/backport/`, `mock_data/reproducer/`, or
`mock_data/mr_consolidation/`:

```json
{
  "rhel_config_override": {
    "current_y_streams": {
      "9": "rhel-9.10",
      "10": "rhel-10.2"
    },
    "current_z_streams": {
      "8": "rhel-8.10.z",
      "9": "rhel-9.8.z",
      "10": "rhel-10.1.z"
    },
    "upcoming_z_streams": {
      "9": "rhel-9.9.z",
      "10": "rhel-10.2.z"
    }
  }
}
```

This example records a complete configuration snapshot for one test case.
Copy the corresponding values from the `rhel-config.json` used when creating
the fixture, rather than using the streams current at a later date.

Values that are not included in the override use the general
`rhel-config.json` (cases without `rhel_config_override` use that file
unchanged).
