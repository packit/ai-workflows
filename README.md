# AI workflows driven by Goose AI

For Goose AI to be able to access Jira tickets you need an MCP Server.
In this workflows we are using [MCP server for Atlassian tools](https://github.com/sooperset/mcp-atlassian).

## Configure

1. Copy `env.template` to `.env` and open the newly created `.env` file.
2. Set your Gemini key in `GOOGLE_API_KEY` (take it from Google Cloud -> API & Services -> Credentials -> API Keys -> show key)
3. Set your Jira Personal Token in `JIRA_PERSONAL_TOKEN` (create PATs in your Jira/Confluence profile settings - usually under "Personal Access Tokens")
4. Change (if needed) the `JIRA_URL` now pointing at `https://issues.redhat.com/`

If you need to change the llm provider and model, they are stored in the goose config file: `goose-container/goose-config.yaml` (`GOOSE_PROVIDER`, `GOOSE_MODEL`)

## Instructions

Run `make help` to see the instructions on how to run individual MCP servers and run Goose Recipes.
