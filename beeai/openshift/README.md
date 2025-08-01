# OpenShift Deployment

## Steps to deploy:

- Ensure secrets exist for the following values:
  
  `beeai-agent-secrets`:
  ```
    CHAT_MODEL
    GEMINI_API_KEY
    GITLAB_TOKEN
    GITLAB_USER
  ```

  `mcp-atlassian-secret`:
  ```
  JIRA_PERSONAL_TOKEN
  JIRA_URL
  ```
  
  Values of these secrets are documented in [README](https://github.com/packit/jotnar?tab=readme-ov-file#service-accounts--authentication).

- Run `make deploy`. This would apply all the existing configurations to the project.

- Run `oc get route phoenix` and verify url listed in `HOST/PORT` column is accessible.
