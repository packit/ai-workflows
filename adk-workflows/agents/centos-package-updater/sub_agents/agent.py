from google.adk.agents import Agent
from typing import Dict, Any
import os

def get_config() -> Dict[str, Any]:
    return {
        'package': os.environ.get('PACKAGE', ''),
        'version': os.environ.get('VERSION', ''),
        'jira_issues': os.environ.get('JIRA_ISSUES', ''),
        'git_url': os.environ.get('GIT_URL', 'https://gitlab.com/redhat/centos-stream/rpms'),
        'dist_git_branch': os.environ.get('DIST_GIT_BRANCH', 'c10s'),
        'git_user': os.environ.get('GIT_USER', 'RHEL Packaging Agent'),
        'git_email': os.environ.get('GIT_EMAIL', 'rhel-packaging-agent@redhat.com'),
        'gitlab_user': os.environ.get('GITLAB_USER', 'vrothberg')
    }

def create_version_checker_prompt(config: Dict[str, Any]) -> str:
    """Creates the prompt for the version checker agent."""

    return f"""**Context**
You are a CentOS package version checker agent. Your sole responsibility is to determine if a package needs to be updated to a newer version.

**Your Task**
Check if the {config['package']} package needs to be updated from its current version to version {config['version']}.

**Instructions**
1. **Package Location**: Find the {config['package']} package at {config['git_url']}/{config['package']}
2. **Version Detection**:
   - Check the current version in the 'Version' field of the RPM .spec file
   - Do NOT clone the repository just for version detection
   - Use web interface or API calls to read the .spec file
3. **Version Comparison**:
   - Compare current version with target version {config['version']}
   - Determine if update is needed
4. **Decision Making**:
   - If versions are the same: Output "NO_UPDATE_NEEDED" and explain why
   - If update is needed: Output "UPDATE_REQUIRED" with current and target versions
   - If package not found or error: Output "ERROR" with details

**Required Information**
- Package: {config['package']}
- Target Version: {config['version']}
- Repository: {config['git_url']}/{config['package']}
- Branch: {config['dist_git_branch']}

**Output Format**
Provide a clear decision with format:
```
DECISION: [NO_UPDATE_NEEDED|UPDATE_REQUIRED|ERROR]
CURRENT_VERSION: [detected version or N/A]
TARGET_VERSION: {config['version']}
REASON: [brief explanation]
```

**Important Notes**
- Be precise about version comparison
- Do not perform any updates - only check versions
- Focus on accuracy and clear communication
- Handle edge cases (package not found, spec file issues, etc.)
"""

def create_package_updater_prompt(config: Dict[str, Any]) -> str:
    """Creates the prompt for the package updater agent."""

    return f"""**Context**
You are a CentOS package updater agent. You receive confirmation that an update is needed and perform the actual package update process.

**Your Task**
Update the {config['package']} package to version {config['version']} and prepare it for merge request.

**Prerequisites**
You should only run if the previous agent confirmed "UPDATE_REQUIRED". If not, stop immediately.

**Core Rules and Guidelines**
- Package repository: {config['git_url']}/{config['package']}
- Git user: {config['git_user']} ({config['git_email']})
- GitLab user: {config['gitlab_user']}
- Work in temporary directories (mktemp)
- Follow Fedora packaging guidelines: https://docs.fedoraproject.org/en-US/packaging-guidelines/
- Reference RPM guide: https://rpm-packaging-guide.github.io/
- Do NOT run `centpkg new-sources` - only document commands

**Update Process**
1. **Repository Setup**:
   - Clone the package to temporary directory

2. **Package Update**:
   - Create branch: `automated-packaging-update-{config['version']}`
   - Update 'Version' and 'Release' fields in .spec file
   - Update version-related macros if present
   - Create changelog entry: "Resolves: <jira_issue>" for each in {config['jira_issues']}
   - Download sources: `spectool -g -S {config['package']}.spec`
   - Document upload command: `centpkg --release {config['dist_git_branch']} new-sources`

3. **Validation**:
   - Run `rpmlint` on .spec file changes
   - Generate SRPM: `rpmbuild -bs`
   - Fix any validation errors

4. **Commit Preparation**:
   - Title: "[DO NOT MERGE: AI EXPERIMENTS] Update to version {config['version']}"
   - Include JIRA references for each issue in {config['jira_issues']}
   - Include build status and suggestions
   - Do NOT push (testing mode)

**Required Parameters**
- Package: {config['package']}
- Version: {config['version']}
- JIRA Issues: {config['jira_issues']}
- Git URL: {config['git_url']}
- Branch: {config['dist_git_branch']}

**Response Format**
Provide detailed output:
- Step-by-step execution log
- Commands executed/documented
- Validation results
- Final commit message
- Summary of changes

**Safety**
- Work in isolated directories
- Document dangerous operations instead of executing
- Provide audit trail
- Never push commits automatically
"""

# Create the sub-agents
version_checker_agent = Agent(
    model="gemini-2.5-pro-preview-05-06",
    name="version_checker",
    description="Checks if a CentOS package needs version update",
    instruction=create_version_checker_prompt(get_config()),
    output_key="version_check_result"
)

package_updater_agent = Agent(
    model="gemini-2.5-pro-preview-05-06",
    name="package_updater",
    description="Performs the actual package update and prepares merge request",
    instruction=create_package_updater_prompt(get_config()),
    output_key="update_result"
)
