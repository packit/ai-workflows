# AI workflows, driven by Goose AI

For Goose AI to be able to access Jira tickets you need an MCP Server.
In this workflows we are using [MCP server for Atlassian tools](https://github.com/sooperset/mcp-atlassian).

## Configure

Secrets such as tokens are managed in the form of environment files.  The templates for those files can be found in the `./templates` directory.  To configure the deployment, run `make config` first to copy the files to the `.secrets` directory where you can manually edit the files to add your tokens and more. This step also sets up a .env file in the toplevel directory.


 `GOOGLE_API_KEY`: take it from Google Cloud -> API & Services -> Credentials -> API Keys -> show key)
`JIRA_PERSONAL_TOKEN`: create PATs in your Jira/Confluence profile settings - usually under "Personal Access Tokens"
`GITLAB_TOKEN` with read permissions (read_user, read_repository, read_api).  Note that some recipes require write access to Gitlab: use it at your own risk.
`TESTING_FARM_API_TOKEN`: required and can be generated from https://testing-farm.io/tokens/.

If you need to change the llm provider and model, they are stored in the Goose config file: `goose-container/goose-config.yaml` (`GOOSE_PROVIDER`, `GOOSE_MODEL`)

## Build

- `make build` This builds the goose image using an officially released goose binaries
- `make debug-build` This builds the goose image using a rebuild of goose from git. `*.patch` in `goose-container/` are applied to the git tree before building.
## Run Goose - interactively - with the MCP Atlassian server

To run goose interactively, don't be tempted to run `podman compose up` or similar, because input from your terminal might not be directed to the Goose container. Instead use:

1. `make run-goose` (Requires enabling the [Generative Language API](https://console.developers.google.com/apis/api/generativelanguage.googleapis.com/) or another LLM provider and configuring the environment variables as described in the [configuration docs](https://block.github.io/goose/docs/guides/config-file#global-settings).)
2. Type *List all In Progress issues at https://issues.redhat.com/projects/LD* and wait for the output.
3. `make clean`

You can further manually run test and run the Goose recipes which are mounted into the container at `/home/goose/recipes`.

## Run local Goose recipes

The recipes are defined in `goose-recipes/`.  If you want to run `goose-recipes/<recipe>.yaml`, run the following:

1. `make <recipe>`
2. `make clean`

## Development

This project uses [pre-commit](https://pre-commit.com/) hooks. To set up:

```bash
pip install pre-commit
pre-commit install
```

## Container Images

Container images are available at the [jotnar](https://quay.io/organization/jotnar) organization on quay.io.

## Production

This project is deployed in the `jotnar-prod` namespace on the Cyborg Openshift cluster. Members of `jotnar` LDAP group have admin access to it.
