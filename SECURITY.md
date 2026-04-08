# Security

## Preventing Accidental Secret Commits

The following file patterns are excluded from version control via `.gitignore` to prevent
accidental exposure of sensitive credentials:

```
*.key
*.pem
*.p12
*.keytab
.env
.secrets
```

[detect-secrets](https://github.com/Yelp/detect-secrets) is configured as a pre-commit hook to
scan staged files for potential secrets before each commit.

Our secrets management procedures are described in this document: https://github.com/packit/jotnar/blob/main/access_control.md

To report a security vulnerability in this project, open a private security advisory on GitHub or
contact the maintainers directly.
