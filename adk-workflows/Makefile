IMAGE_NAME ?= adk-agent
CONTAINER_NAME ?= adk-agent-container
COMPOSE_FILE ?= compose.yaml

# Detect compose command (podman compose vs docker-compose)
COMPOSE ?= $(shell command -v podman >/dev/null 2>&1 && echo "podman compose" || echo "docker-compose")

.PHONY: build
build:
	$(COMPOSE) -f $(COMPOSE_FILE) build

.PHONY: start-mcp
start-mcp:
	$(COMPOSE) -f $(COMPOSE_FILE) up -d mcp-atlassian

.PHONY: stop-mcp
stop-mcp:
	$(COMPOSE) -f $(COMPOSE_FILE) stop mcp-atlassian

.PHONY: run-adk-bash
run-adk-bash:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm adk-agent /bin/bash

.PHONY: workflow
workflow:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e AGENT_TYPE=workflow \
		adk-agent /bin/sh -c "/wait_mcp_server.sh && cd agents/centos-package-updater/sub_agents && python run_agent.py"

.PHONY: issue-details
issue-details:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e AGENT_TYPE=issue_analyzer \
		adk-agent /bin/sh -c "/wait_mcp_server.sh && cd agents/centos-package-updater/sub_agents && python run_agent.py"

.PHONY: rebase-package
rebase-package:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e VERSION=$(VERSION) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e AGENT_TYPE=package_updater \
		adk-agent /bin/sh -c "/wait_mcp_server.sh && cd agents/centos-package-updater/sub_agents && python run_agent.py"

.PHONY: clean
clean:
	$(COMPOSE) -f $(COMPOSE_FILE) down --volumes

help:
	@echo "Available ADK targets:"
	@echo "  build                       - Build ADK container image"
	@echo "  start-mcp                   - Start MCP Atlassian server"
	@echo "  stop-mcp                    - Stop MCP Atlassian server"
	@echo "  run-adk                     - Start ADK agent (interactive)"
	@echo "  run-adk-bash                - Run ADK container with bash shell"
	@echo "  workflow                    - Run full CentOS package workflow"
	@echo "  issue-details               - Run issue analyzer sub-agent"
	@echo "  rebase-package              - Run package updater sub-agent"
	@echo "  clean                       - Stop services and remove containers"
	@echo ""
	@echo "Usage examples:"
	@echo "  make build"
	@echo "  make run-adk"
	@echo "  make workflow JIRA_ISSUE=\"RHEL-123\""
	@echo "  make issue-details JIRA_ISSUE=\"RHEL-123\""
	@echo "  make rebase-package PACKAGE=httpd VERSION=2.4.58"
	@echo ""
	@echo "Environment variables:"
	@echo "  COMPOSE                     - Compose command (auto-detected: $(COMPOSE))"
	@echo "  COMPOSE_FILE                - Compose file (default: $(COMPOSE_FILE))"
