# AI Workflows Platform

An AI automation platform that leverages multiple AI agent frameworks for Red Hat engineering workflows. This repository uses **Goose AI**, **BeeAI**, and **ADK Workflows** to provide automation for RHEL/CentOS package management, issue triage, and development workflows.

## 🏗️ Architecture Overview

This platform consists of several integrated components:

### AI Agents
- **[Goose AI](./goose/)** - Driven by human language instructions that call out to tools backed by MCP servers and the shell
- **[BeeAI Framework](./beeai/)** - Driven by python scripts that call out to tools backed by MCP servers
- **[ADK Workflows](./adk-workflows/)** - Also driven by python

### MCP (Model Context Protocol) Servers
- **Atlassian MCP Server** - Jira/Confluence integration for issue management
- **Testing Farm MCP Server** - Integration with Testing Farm for running packaging tests

### Package Analysis Tools
- **[Package Dependency Analyzer](./scripts/find-package-dependents.py)** - Script for finding reverse dependencies

### Automation Recipes
- **[Goose Recipes](./goose-recipes/)** - Predefined workflows for common tasks
- **Issue Triage** - Automated analysis and routing of RHEL issues
- **Package Rebase** - Automated package version updates
- **Backport Management** - Automated patch application workflows
- **Reverse Dependency Testing** - Automated testing of select reverse dependencies based on context

## 🚀 Quick Start

### Prerequisites
- Podman, podman-compose
- Make
- API tokens (see Configuration section)

### Initial Setup
1. **Configure environment:**
   ```bash
   ❯ make config
   ```
   This copies template files to `.secrets/` for manual configuration.

2. **Set up API tokens:**
   - `GOOGLE_API_KEY` - From Google Cloud Console
   - `JIRA_PERSONAL_TOKEN` - From Jira profile settings
   - `GITLAB_TOKEN` - With appropriate read/write permissions
   - `TESTING_FARM_API_TOKEN` - From https://testing-farm.io/tokens/

3. **Build the platform:**
   ```bash
   ❯ make build
   ```

### Running Different Components

#### Interactive Goose AI Session
```bash
❯ make run-goose
```

#### BeeAI Automated Workflows
See beeai/README.md

#### ADK Package Automation
See adk-workflows/README.md

#### Goose Recipe Execution
```bash
# Run specific automation recipes
❯ make triage-issue
❯ make backport-fix
❯ make rebase-package
❯ make test-reverse-dependencies PACKAGE=systemd CHANGE='Fix bug in hostnamed that caused avahi to crash'
```

## 📋 Available Workflows

### Package Management
- **Issue Triage** - Automatically analyze JIRA issues and determine resolution path
- **Package Rebase** - Update packages to newer upstream versions
- **Backport Fixes** - Apply specific patches to packages
- **Dependency Analysis** - Package dependency mapping

### Development Automation
- **Repository Management** - Automated Git operations and merge requests
- **Testing Integration** - Automated testing via Testing Farm
- **Documentation Generation** - Automated documentation updates

### Monitoring & Observability
- **Phoenix Web Interface** - beeai agent tracing at http://localhost:6006/
- **Redis Commander** - beeai queue monitoring at http://localhost:8081/

## 🔧 Configuration

### LLM Provider Configuration
Edit `goose-container/goose-config.yaml` to configure:
- `GOOSE_PROVIDER` - Your preferred LLM provider
- `GOOSE_MODEL` - Specific model to use

### Dry Run Mode
Enable safe testing without actual changes:
```bash
❯ export DRY_RUN=true
```

## 📁 Repository Structure

```
ai-workflows/
├── goose/                    # Goose AI agent framework
├── beeai/                    # BeeAI framework with specialized agents
├── adk-workflows/            # Google ADK automation workflows
├── goose-recipes/            # Predefined automation workflows
├── scripts/                  # Utility scripts and tools
├── templates/                # Configuration templates
├── goose-container/          # Container configuration for Goose
└── compose.yaml              # Docker Compose orchestration
```

## 🤖 Agent Capabilities

### BeeAI Agents
- **Triage Agent** - Analyzes JIRA issues and routes to appropriate resolution
- **Rebase Agent** - Automatically updates packages to newer versions
- **Backport Agent** - Applies targeted fixes and patches

### Goose AI Integration
- Check JIRA tickets for rebase requests
- Get details of JIRA issues
- Analyze JIRA ticket to decide what automation (if any) is appropriate
- Backport fix from upstream
- Test package in testing farm
- Test reverse dependencies of package in testing farm

## 🚢 Production Deployment

### Container Images
Available at [jotnar organization on quay.io](https://quay.io/organization/jotnar)

### OpenShift Deployment
- **Namespace**: `jotnar-prod` on Cyborg OpenShift cluster
- **Access**: Members of `jotnar` LDAP group have admin access
- **Monitoring**: Integrated observability and logging

## 📖 Documentation

- [BeeAI Framework Details](./beeai/README.md)
- [ADK Workflows Guide](./adk-workflows/README.md)
- [Goose AI Documentation](./goose/README.md)
- [Package Analysis Tools](./scripts/README.md)

## 🤝 Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines.

**Merging Policy**: Prefer rebase-merging over merge commits unless preserving branch history is necessary.
