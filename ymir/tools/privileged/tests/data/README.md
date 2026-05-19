# Where are the Jira files?
This directory is meant to contain Jira mock files from
`git@gitlab.cee.redhat.com:jotnar-project/testing-jiras.git` if you have the access.

Clone the repository and copy (or symlink) the contents of its `jiras/` directory here.
If you want agents to be able to write to them, add the writing permission bit for all users.

The expected layout of the private repository is:

```
.
├── jiras
│   ├── RHEL-112546
│   ├── RHEL-114607
│   ├── RHEL-15216
│   ├── RHEL-29712
│   └── RHEL-61943
├── mock_data
│   ├── RHEL-112546.json
│   ├── RHEL-114607.json
│   ├── RHEL-15216.json
│   ├── RHEL-29712.json
│   └── RHEL-61943.json
└── README.md
```
