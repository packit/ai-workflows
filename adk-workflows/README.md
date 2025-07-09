# ADK CentOS Package Updater Agent

This Google ADK agent automates CentOS package updates and prepares merge requests.

## Quick Start

1. **Build container:**
   ```bash
   make build
   ```

2. **Configure environment:**
   ```bash
   cp env.template .env
   # Edit .env with your API keys and package details
   ```

3. **Run specific workflows:**
   ```bash
   # Full workflow
   make workflow JIRA_ISSUE="RHEL-123"

   # Individual components
   make issue-details JIRA_ISSUE="RHEL-123"
   make rebase-package PACKAGE=httpd VERSION=2.4.58

   ```
