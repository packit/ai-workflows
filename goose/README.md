# Goose AI Agent Framework

This directory contains all Goose AI related components for the AI Workflows platform.

## Container Builds

The `container/Containerfile` provides several build targets:

- **production** (default): Official Goose releases
- **debug**: Custom Goose builds with patches applied
- **source-build**: Build Goose from source with custom patches

## 🍳 Automation Recipes

The `recipes/` directory contains YAML workflows for common RHEL engineering tasks:

- **Issue Management**: Triage and analyze JIRA issues
- **Package Operations**: Rebase packages, apply backport fixes
- **Testing**: Run package tests and reverse dependency checks
- **Repository Management**: Check tickets and analyze dependencies

## 🚀 Usage

Navigate to the goose directory and use the Makefile:

```bash
# Build the containers
make build

# Run interactive Goose session
make run-goose

# Run specific recipes
make triage-issue ISSUE=RHEL-12345
make backport-fix PACKAGE=systemd BACKPORT_FIX="Fix memory leak"
make rebase-package PACKAGE=curl VERSION=8.0.1
```

## ⚙️ Configuration

Edit `container/goose-config.yaml` to configure:
- LLM provider and model settings
- MCP server connections
- Tool configurations

## 🛠️ Setup

1. **Copy template files:**
   ```bash
   make config
   ```

2. **Configure your environment variables in `.secrets/`:**
   - `goose.env` - Goose and API configurations
   - `mcp-atlassian.env` - Jira/Confluence access
   - `mcp-testing-farm.env` - Testing Farm API token

3. **Build and run:**
   ```bash
   make build
   make run-goose
   ```

See the main repository README for detailed setup instructions.
