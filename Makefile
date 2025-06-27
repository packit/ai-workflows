COMPOSE ?= podman compose

.PHONY: build
build:
	$(COMPOSE) build

.PHONY: run-mcp-atlassian
run-mcp-atlassian:
	$(COMPOSE) up -d mcp-atlassian

.PHONY: stop-mcp-atlassian
stop-mcp-atlassian:
	$(COMPOSE) down mcp-atlassian

.PHONY: logs-mcp-atlassian
logs-mcp-atlassian:
	$(COMPOSE) logs -f mcp-atlassian

.PHONY: run-goose
run-goose:
	$(COMPOSE) run --rm goose

.PHONY: run-goose-bash
run-goose-bash:
	$(COMPOSE) run --rm --entrypoint /usr/bin/bash goose

PROJECT ?= RHEL
COMPONENT ?= cockpit
.PHONY: check-jira-tickets
check-jira-tickets:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/home/goose/wait_mcp_server.sh && /usr/local/bin/goose run --recipe recipes/check-jira-tickets.yaml \
			--params project=$(PROJECT) \
			--params component=$(COMPONENT)"

ISSUE ?= RHEL-78418
.PHONY: issue-details
issue-details:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/home/goose/wait_mcp_server.sh && /usr/local/bin/goose run --recipe recipes/issue-details.yaml \
			--params issue=$(ISSUE)"

PACKAGE ?= cockpit
VERSION ?= 339
JIRA_ISSUES ?= "RHEL-123"
.PHONY: rebase-package
rebase-package:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "set -e; \
			set +x; \
			askpass=\"\$$(mktemp)\"; \
			echo '#!/bin/sh' > \"\$$askpass\"; \
			echo 'echo \$$GITLAB_TOKEN' >> \"\$$askpass\"; \
			chmod +x \"\$$askpass\"; \
			export GIT_ASKPASS=\"\$$askpass\"; \
			/home/goose/wait_mcp_server.sh && /usr/local/bin/goose run --recipe recipes/rebase-package.yaml \
			--params package=$(PACKAGE) \
			--params version=$(VERSION) \
			--params jira_issues=$(JIRA_ISSUES) \
			--params gitlab_user=$(GITLAB_USER) && echo 'Recipe completed. Dropping into shell...' && /bin/bash"

PACKAGE ?= podman
ARCH    ?= x86_64
.PHONY: reverse-dependencies
reverse-dependencies:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/reverse-dependencies.yaml \
			--params package=$(PACKAGE) \
			--params arch=$(ARCH)"

.PHONY: clean
clean:
	$(COMPOSE) down
	podman volume prune -f

help:
	@echo "Available targets:"
	@echo "  build                       - Build all images"
	@echo "  run-mcp-atlassian           - Start MCP server in background"
	@echo "  stop-mcp-atlassian          - Stop MCP server"
	@echo "  logs-mcp-atlassian          - Show MCP server logs"
	@echo "  run-goose                   - Run goose interactively"
	@echo "  run-goose-bash              - Run goose with bash shell"
	@echo "  <recipe>                    - To run the recipes/<recipe>.yaml"
	@echo "  clean                       - Stop all services and clean volumes"
