# AI Workflows and llama-stack

AI-powered Jira issue analysis using Llama Stack and MCP Atlassian server.

This is a **quick** research on how to run our prompts using llama stack.

## Cons:
   - Complexity especially if compared with Goose.
   - I couldn't experiment with the containers. You need to build your own server distribution to run the llama server in a container.
   I tried multiple existing images but none had everything I need. 
   - The configuration is ready to use the **MCP Jira server**, but you need to change this line:
      ```diff
      -        jira_agent = Agent(client, **jira_config)
      +        jira_agent = Agent(client, tools=["mcp::jira"], **jira_config)
      ```
   I can see requests going to the MCP Jira server from the Llama Stack (with a 200 status code). However, after that the llama stack server stops with the following error (as if it is invoking the tool without the needed parameters - I can go a bit further with specifying the tool binding Agent(client, tools=[{"name": "mcp::jira/jira_get_issue", "args": {"issue_key": issue}}], **jira_config) - but then again I have an error because the request has a session id parameter that the mcp server does not expect)
   ```
litellm.exceptions.BadRequestError: litellm.BadRequestError: VertexAIException BadRequestError - b'{\n  "error": {\n    "code": 400,\n    "message": "* GenerateContentRequest.tools[0].function_declarations[15].parameters.properties[fields].items: missing field.\\n* GenerateContentRequest.tools[0].function_declarations[15].parameters.properties[issue_ids_or_keys].items: missing field.\\n",\n    "status": "INVALID_ARGUMENT"\n  }\n}\n'   
   ```


## Pros:
   - The feedback from Llama Stack is much more understandable/readable than the one from Goose. Also, while experimenting with Llama Stack, I never experienced hanging issues (as with Goose) or performance problems.
   - Multiple solutions are already available. For example, you can use the llama session management instead of a custom Redis queue mechanism. In the dir `session-based-container-workflow` there is an example generated with Cursor but not tested yet (since the *llama-stack image* does not exist, I generated that code to showcase the usage of pre-built session management in Llama Stack compared to a queue implemented with Redis).
   - Most of the configuration is done in a declarative way (YAML config file). The `workflow_runner.py` Python file is needed only for chaining the agents with the preferred workflow style, see https://github.com/meta-llama/llama-stack/blob/main/docs/notebooks/Llama_Stack_Agent_Workflows.ipynb.
   - Even though the server approach is complex to set up, it has its advantages (a llama-stack agent can be used without the server, but you would lose these advantages):
      - Configuration: The YAML server configuration is quite simple; without the server, you need to set up the providers through Python code.
      - Resource management: Without the server, the agents are responsible for managing memory, models, and connections to external services.
      - Scalability: The server can distribute load across multiple instances.
      - State management: When using the server, the sessions are persistent across multiple requests.
      - Development: The server supports config changes without restarts, provides built-in metrics and logging, and handles multiple users/developers.
      - Feature limitations: Without the server, there is no routing between providers, built-in safety guards, automatic failover between models, or complex workflow orchestration.
      


## Setup

1. Copy `env.template` to `.env` and configure:
   - `GOOGLE_API_KEY` - Your Gemini API key
   - `JIRA_PERSONAL_TOKEN` - Your Jira Personal Access Token
   - `JIRA_URL` - Your Jira instance URL (default: https://issues.redhat.com)

2. Bootstrap the virtual environment:
   ```bash
   make bootstrap
   ```

   Or manually install Llama Stack:
   ```bash
   python -m venv ~/venv/ai-workflows
   source ~/venv/ai-workflows/bin/activate
   pip install llama-stack
   ```
   
   > **Note**: The default virtual environment name is `ai-workflows`. You can customize this by setting the `VENV` variable: `make bootstrap VENV=my-custom-venv`

## Usage

### Available Targets

#### `make bootstrap`
Bootstrap the virtual environment and install dependencies.
- Creates virtual environment at `~/venv/ai-workflows` (or custom name)
- Installs Llama Stack and any additional requirements
- One-time setup step

#### `make mcp-server`
Start the Atlassian MCP server using podman-compose.
- Runs in background on port 9000
- Required for Jira integration

#### `make llama-server`
Start the Llama Stack server locally.
- Runs on port 8321
- Requires MCP server to be running first
- Uses configuration from `llama_stack_config.yaml`

#### `make workflow-runner`
Run the Jira analysis and the rebase agent locally with prompt chaining.
- Requires Llama Stack server to be running
- Default issue: RHEL-78418
- Custom issue: `make workflow-runner ISSUE=YOUR-ISSUE-ID`

#### `make run-all`
Run all services in the correct sequence.
- Starts MCP server first
- Then starts Llama Stack server
- Instructions provided for running agent in separate terminal

#### `make clean`
Clean up all services.
- Stops and removes compose services with orphans
- Removes leftover MCP and Llama Stack containers  
- Prunes volumes and networks
- Kills local Llama Stack processes
- Checks for port conflicts (9000, 8321)

#### `make clean-all`
Aggressive cleanup (use with caution!).
- Removes ALL podman containers, images, and networks
- Stops all running processes
- Complete system reset

## Examples

```bash
# First time setup
make bootstrap

# Start everything in sequence
make run-all

# Or run services separately:
make mcp-server
make llama-server    # In terminal 1
make workflow-runner  # In terminal 2

# Analyze specific issue
make workflow-runner ISSUE=RHEL-12345

# Clean up when done
make clean

```