# AI workflows, driven by Goose AI, related with Jira tickets.

For Goose AI to be able to access Jira tickets you need an MCP Server.
In this workflows we are using [MCP server for Atlassian tools](https://github.com/sooperset/mcp-atlassian).

## Configure

1. Copy `.env.template` in `.env` and open the newly created `.env` file.
2. Set your Gemini key in `GOOGLE_API_KEY` (take it from Google Cloud -> API & Services -> Credentials -> API Keys -> show key)
3. Set your Jira Personal Token in `JIRA_PERSONAL_TOKEN` (create PATs in your Jira/Confluence profile settings - usually under "Personal Access Tokens")
4. Change (if needed) the `JIRA_URL` now pointing at `https://issues.redhat.com/`

If you need to change the llm provider and model, they are stored in the goose config file: `files/goose-config.yaml` (`GOOSE_PROVIDER`, `GOOSE_MODEL`)

If you want to use Goose AI with remote recipes, set the repo from where to take the recipes in `files/goose-config.yaml` -> `GOOSE_RECIPE_GITHUB_REPO` with `username/repo`. Use the command `make run-goose-workflow` to run the recipe.
*Warning: while developing it is difficult to use a remote recipe (since it has not yet been deployed in main or merged in target repo, I have found no way to dinamically set a branch or fork for playing with the recipe).*

## Build

`make build`

## Run goose - interactively - with the mcp atlassian server

Run mcp server and goose separately, otherwise not all the input from your terminal 
is always redirected to the goose container.

1. `make run-mcp-server`
2. `make run-goose`
3. Type *List all In Progress issues at https://issues.redhat.com/projects/LD* and wait for the output.
4. `make clean`

## Run local goose workflow

The workflow is defined in `recipes/check-jira-tickets/recipe.yaml`.

1. `PROJECT=RHEL COMPONENT=cockpit make run-goose-local-workflow`
2. `make clean`
