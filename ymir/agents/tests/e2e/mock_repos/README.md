# Where are the mock data files?
This directory is meant to contain mock repo fixture files from
`git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git` if you have the access.

Clone the repository and copy (or symlink) the contents of its `mock_data/` directory here.

The expected layout of the private repository is:

```
.
├── jiras
│   ├── RHEL-112546
│   ├── RHEL-15216
│   ├── RHEL-29712
│   └── RHEL-61943
├── mock_data
│   ├── RHEL-112546.json
│   ├── RHEL-15216.json
│   ├── RHEL-29712.json
│   └── RHEL-61943.json
└── README.md
```

Each JSON file in `mock_data/` describes the repos to clone at a pre-fix state
and an optional z-stream override for a given Jira issue. See
`ymir/common/mock_repos.py` for the schema documentation.
