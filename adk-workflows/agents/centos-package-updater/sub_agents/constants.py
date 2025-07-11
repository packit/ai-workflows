#!/usr/bin/env python3
"""
Constants for CentOS package workflow agent.
Contains JIRA comment templates and other configuration constants.
"""

# JIRA Comment Templates
JIRA_COMMENT_REBASE_TEMPLATE = """🤖 **AI Package Workflow Completed**

**Decision:** Rebase
**Package:** {package}
**Version:** {version}
**Branch:** {branch}

**Analysis:** {findings}"""

JIRA_COMMENT_BACKPORT_TEMPLATE = """🤖 **AI Package Workflow Completed**

**Decision:** Backport
**Package:** {package}
**Patch URL:** {patch_url}
**Branch:** {branch}
**Justification:** {justification}

**Analysis:** {findings}"""

JIRA_COMMENT_OTHER_TEMPLATE = """🤖 **AI Package Workflow Completed**

**Decision:** {decision}
**Result:** {result}

**Analysis:** {findings}"""

JIRA_COMMENT_FAILURE_TEMPLATE = """🤖 **AI Package Workflow Failed**

**Error:** {error_message}

**Issue:** {jira_issue}
**Status:** Failed during execution

Please check the logs for more details and retry the workflow."""

# Default values for template formatting
DEFAULT_VALUES = {
    'package': 'unknown',
    'version': 'N/A',
    'branch': 'N/A',
    'patch_url': 'N/A',
    'justification': 'N/A',
    'findings': 'No findings available',
    'result': 'Workflow completed'
}
