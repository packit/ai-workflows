# OpenShift Deployment

## Steps to deploy:

- Ensure secrets exist for the following values:

  `gitlab-env`:
  ```
  GITLAB_TOKEN
  ```

  `jira-env`:
  ```
  JIRA_TOKEN
  ```

  `jotnar-bot-keytab`:
  ```
  oc create secret generic jotnar-bot-keytab --from-file=jotnar-bot.keytab
  ```

  `testing-farm-env`:
  ```
  TESTING_FARM_API_TOKEN
  ```

  Values of these secrets are documented in [README](https://github.com/packit/jotnar?tab=readme-ov-file#service-accounts--authentication).

- Create RHEL configuration ConfigMap manually:

  ```bash
  # Get rhel-config.json from Bitwarden (contains info about RHEL versions)
  # Then create ConfigMap:
  oc create configmap rhel-config --from-file=rhel-config.json
  ```

  The `rhel-config.json` file is stored in [jotnar](https://github.com/packit/jotnar) repo.

- Create Vertex AI secret:

  ```bash
  oc create secret generic vertex-key --from-file=jotnar-vertex-prod.json
  ```
  You can obtain the file from our bitwarden.

- Run `make deploy`. This would apply all the existing configurations to the project.

- Run `oc get route phoenix` and verify url listed in `HOST/PORT` column is accessible.

## Jira Issue Fetcher Deployment

Manually run jira issue fetcher:

```bash
make run-jira-issue-fetcher
```
