from google.adk.agents import Agent
from google.adk.tools import agent_tool, google_search
from typing import Dict, Any
import os
from shell_utils import shell_command
from google.genai.types import GenerateContentConfig


def get_model() -> str:
    """Get model from environment with consistent default."""
    return os.environ.get('MODEL', 'gemini-2.5-pro')

def get_backport_config() -> Dict[str, Any]:
    """Get configuration for the backport agent."""
    return {
        'package': os.environ.get('PACKAGE_NAME', os.environ.get('PACKAGE', '')),
        'jira_issue': os.environ.get('JIRA_ISSUE', ''),
        'git_url': os.environ.get('GIT_URL', 'https://gitlab.com/redhat/centos-stream/rpms'),
        'dist_git_branch': os.environ.get('DIST_GIT_BRANCH', 'c10s'),
        'upstream_fix': os.environ.get('UPSTREAM_FIX', os.environ.get('PATCH_URL', '')),
        'gitlab_user': os.environ.get('GITLAB_USER', ''),
        'git_user': os.environ.get('GIT_USER', 'RHEL Packaging Agent'),
        'git_email': os.environ.get('GIT_EMAIL', 'rhel-packaging-agent@redhat.com'),
        'model': get_model(),
    }

def create_backport_prompt(config: Dict[str, Any]) -> str:
    """Creates the prompt for the backport agent based on goose-recipes/backport-fix.yaml."""

    return f"""**Context**
You are an agent for backporting a fix for a CentOS Stream package. You will prepare the content of the update
and then create a commit with the changes. Create a temporary directory and always work inside it.

**IMPORTANT GUIDELINES**
- **Tool Usage**: You have shell_command (direct tool for executing shell commands) and SearchAgent (for web search) - use them as needed!
- **No Placeholders**: Use shell_command tool for every suggested command - execute actual commands and provide real results
- **Context Tracking**: Remember directories, files, and commands from previous steps
- **Command Execution Rules**:
   - Use shell_command tool for ALL command execution
   - If a command shows "no output" or empty STDOUT, that is a VALID result - do not retry
   - Commands that succeed with no output (like 'mkdir', 'cd', 'git add') are normal - report success
- **Error Handling**:
   - Show actual error messages, don't give up on first failure
   - Check that the changes you have done make sense and correct yourself

**Configuration**:
- Package: {config['package']}
- JIRA Issue: {config['jira_issue']}
- Git URL: {config['git_url']}
- Branch: {config['dist_git_branch']}
- Upstream Fix: {config['upstream_fix']}
- GitLab User: {config['gitlab_user']}

**STEP-BY-STEP INSTRUCTIONS**

Follow exactly these steps:

**Step 1: Find the package location**
- Find the location of the {config['package']} package at {config['git_url']}
- Always use the {config['dist_git_branch']} branch
- Create a temporary directory: ```mkdir -p /tmp/backport-work && cd /tmp/backport-work```

**Step 2: Check if fix is already applied**
- Check if the package {config['package']} already has the fix {config['jira_issue']} applied
- Look for existing patches or changelog entries that reference the JIRA issue
- If fix is already applied, stop and report this

**Step 3: Create local Git repository**
- Check if the fork already exists for {config['gitlab_user']} as {config['gitlab_user']}/{config['package']}
- If not, create a fork of the {config['package']} package using the glab tool
- Clone the fork using git and HTTPS into the temp directory: ```git clone https://gitlab.com/{config['gitlab_user']}/{config['package']}.git && cd {config['package']}```
- Run command `centpkg sources` in the cloned repository which downloads all sources defined in the RPM specfile
- Create a new Git branch named `automated-package-update-{config['jira_issue']}`

**Step 4: Update the package with the fix**
- Update the 'Release' field in the .spec file as needed (or corresponding macros), following packaging documentation
- Make sure the format of the .spec file remains the same
- Fetch the upstream fix {config['upstream_fix']} locally and store it in the git repo as "{config['jira_issue']}.patch"
- Add a new "Patch:" entry in the spec file for patch "{config['jira_issue']}.patch"
- Verify that the patch is being applied in the "%prep" section
- Create a changelog entry, referencing the Jira issue as "Resolves: {config['jira_issue']}" for the issue {config['jira_issue']}
- The changelog entry has to use the current date and {config['git_user']} <{config['git_email']}>
- **IMPORTANT**: Only perform changes relevant to the backport update: Do not rename variables, comment out existing lines, or alter if-else branches in the .spec file

**Step 5: Verify and adjust the changes**
- Use `rpmlint` to validate your .spec file changes and fix any new errors it identifies
- Generate the SRPM using `rpmbuild -bs` (ensure your .spec file and source files are correctly copied to the build environment as required by the command)
- Verify the newly added patch applies cleanly using the command `centpkg prep`

**Step 6: Commit the changes**
- Use the {config['git_user']} and {config['git_email']} for the commit
- The title of the Git commit should be in the format "[DO NOT MERGE: AI EXPERIMENTS] backport {config['jira_issue']}"
- Include the reference to Jira as "Resolves: {config['jira_issue']}" for the issue {config['jira_issue']}
- Commit the RPM spec file change and the newly added patch file

**Output Format**:

Your output must strictly follow the format below.

STATUS: success | failure

If Success:
    PACKAGE: [package name]
    JIRA_ISSUE: [jira issue]
    PATCH_FILE: [patch file name]
    BRANCH: [git branch]
    COMMIT_HASH: [git commit hash]
    LOGS: [Detailed logs of all operations performed]

If Failure:
    PACKAGE: [package name]
    JIRA_ISSUE: [jira issue]
    ERROR_MESSAGE: [Description of what went wrong]
    LOGS: [Detailed logs including error information]
"""

def create_backport_agent(mcp_tools=None):
    """Factory function to create backport agent."""
    config = get_backport_config()
    model = config['model']

    # Search specialist agent - uses Google search for finding package information
    search_agent = Agent(
        model=model,
        name='SearchAgent',
        instruction='You are a specialist in web search. Search for package information, upstream fixes, patch URLs, documentation and everything else you need.',
        tools=[google_search],
    )

    # Root agent that uses shell_command directly and SearchAgent as wrapped tool
    return Agent(
        name="backport_agent",
        model=model,
        description="Backports fixes for CentOS Stream packages",
        instruction=create_backport_prompt(config),
        tools=[shell_command, agent_tool.AgentTool(agent=search_agent)],
        output_key="backport_result",
        generate_content_config=GenerateContentConfig(temperature=0.4),
    )
