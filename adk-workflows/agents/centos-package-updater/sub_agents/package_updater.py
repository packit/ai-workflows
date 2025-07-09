from google.adk.agents import Agent
from google.adk.tools import agent_tool, google_search
from typing import Dict, Any
import os
from shell_utils import shell_command
from google.genai.types import GenerateContentConfig


def get_model() -> str:
    """Get model from environment with consistent default."""
    return os.environ.get('MODEL', 'gemini-2.5-pro')

def get_config() -> Dict[str, Any]:
    """Get configuration for the combined version checker and package updater agent."""
    return {
        'package': os.environ.get('PACKAGE_NAME', os.environ.get('PACKAGE', '')),
        'version': os.environ.get('PACKAGE_VERSION', os.environ.get('VERSION', '')),
        'branch': os.environ.get('GIT_BRANCH', os.environ.get('DIST_GIT_BRANCH', 'c10s')),
        'jira_issue': os.environ.get('JIRA_ISSUE', ''),
        'git_url': os.environ.get('GIT_URL', 'https://gitlab.com/redhat/centos-stream/rpms'),
        'git_user': os.environ.get('GIT_USER', 'RHEL Packaging Agent'),
        'git_email': os.environ.get('GIT_EMAIL', 'rhel-packaging-agent@redhat.com'),
        'gitlab_user': os.environ.get('GITLAB_USER',''),
        'model': get_model(),
    }

def create_package_updater_prompt(config: Dict[str, Any]) -> str:
    """Creates the prompt for the combined version checker and package updater agent."""

    return f"""**Context**
You are a CentOS package version checker and updater agent. You first check if a package needs updating, then perform the actual package update if required.

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
- **Configuration**:
  - Package repository: {config['git_url']}/[package_name]
  - Git branch: {config['branch']}
  - Available config: PACKAGE="{config['package']}", VERSION="{config['version']}", BRANCH="{config['branch']}", JIRA_ISSUE="{config['jira_issue']}"

**STEP-BY-STEP INSTRUCTIONS**

**Step 1: Get Package Information**
- Look for package info from previous issue_analyzer agent output in conversation
- If not found, use configuration variables: PACKAGE_NAME="{config['package']}", VERSION="{config['version']}", BRANCH="{config['branch']}"
- Verify you have all three values (PACKAGE_NAME, VERSION, BRANCH)
- If missing any values, clearly state what's needed and stop

**Step 2: Check Current Version**
- Use curl the .spec file from the repository and look for "Version:" field in the .spec file
- Compare current version with target VERSION
- **Decision**: Continue if update needed, stop if versions match

**Step 3: Package Update** (Only if update needed)
- Create working directory: ```mkdir -p /tmp/package-update-work && cd /tmp/package-update-work```
- Clone repository: ```cd /tmp/package-update-work && git clone {config['git_url']}/[PACKAGE_NAME].git && cd [PACKAGE_NAME] && ls -la```
- Create update branch: ```cd /tmp/package-update-work/[PACKAGE_NAME] && git checkout -b automated-packaging-update-[VERSION]```
- Update the local package by:
   - Updating the 'Version' and 'Release' fields in the .spec file as needed (or corresponding macros), following packaging documentation.
   - Make sure the format of the .spec file remains the same.
   - Updating macros related to update (e.g., 'commit') if present and necessary; examine the file's history to see how updates are typically done.
      - You might need to check some information in upstream repository, e.g. the commit SHA of the new version.
   - Create changelog entry that will also include "Resolves: {config['jira_issue']}" for each Jira issue, follow the format of the existing changelog entries,
     use {config['git_user']} and {config['git_email']} for the updated changelog entry.
   - Download the remote sources using `spectool -g -S [PACKAGE_NAME].spec` command.
   - IMPORTANT: Only performing changes relevant to the version update: Do not rename variables, comment out existing lines, or alter if-else branches in the .spec file.

**Step 4: Validation** (Only if Step 3 completed)
- Run rpmlint command on the updated .spec file.
- Try to generate an SRPM using the updated .spec file, using `rpmbuild -bs` command (might require defining the source and spec directories).
- Fix any errors.

**Step 5: Commit the changes**
- Use the {config['git_user']} and {config['git_email']} for the commit.
- The title of the Git commit should be in the format "[DO NOT MERGE: AI EXPERIMENTS] Update to version <version>"
- Include the reference to Jira as "Resolves: <jira_issue>" for each issue in.
- Commit just the specfile change.

**Output Format**:

Your output must strictly follow the format below.

STATUS: success | failure

If Success:
    PACKAGE: [package name]
    VERSION: [updated version]
    BRANCH: [git branch]
    COMMIT_HASH: [git commit hash]
    LOGS: [Detailed logs of all operations performed]

If Failure:
    PACKAGE: [package name]
    VERSION: [target version]
    BRANCH: [git branch]
    ERROR_MESSAGE: [Description of what went wrong]
    LOGS: [Detailed logs including error information]
"""

def create_package_updater_agent(mcp_tools=None):
    """Factory function to create package updater agent."""
    config = get_config()
    model = config['model']

    # Search specialist agent - uses Google search for finding package information
    search_agent = Agent(
        model=model,
        name='SearchAgent',
        instruction='You are a specialist in web search. Search for package information, upstream versions, source URLs, documentation and everything else you need.',
        tools=[google_search],
        generate_content_config=GenerateContentConfig(temperature=0.2),
    )

    # Root agent that uses shell_command directly and SearchAgent as wrapped tool
    return Agent(
        name="version_checker_updater",
        model=model,
        description="Checks package versions and performs updates if needed",
        instruction=create_package_updater_prompt(config),
        tools=[shell_command, agent_tool.AgentTool(agent=search_agent)],
        output_key="version_check_and_update_result",
        generate_content_config=GenerateContentConfig(temperature=0.2),
    )
