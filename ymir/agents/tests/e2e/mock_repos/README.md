# Where are the mock data files?

This directory contains mock repo fixture files organized by agent type.
The fixtures come from `git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git`
if you have access.

Clone the repository and copy (or symlink) the contents of its `mock_data/`
directory here, preserving the subdirectory structure.

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
