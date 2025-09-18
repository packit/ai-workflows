## Public make targets
.PHONY: # Help output
.PHONY: help
.PHONY:
.PHONY: # Manage container image
.PHONY: clean build debug-build config
.PHONY:
.PHONY: # Manage MCP server for JIRA
.PHONY: stop-mcp-atlassian run-mcp-atlassian logs-mcp-atlassian
.PHONY:
.PHONY: # Manage MCP server for Testing Farm
.PHONY: run-mcp-testing-farm stop-mcp-testing-farm logs-mcp-testing-farm
.PHONY:
.PHONY: # Start Goose container without a specific recipe
.PHONY: run-goose run-goose-bash
.PHONY:
.PHONY: # Run Goose with specific recipes
.PHONY: check-jira-tickets
.PHONY: issue-details
.PHONY: rebase-package
.PHONY: reverse-dependencies
.PHONY: test-package
.PHONY: test-reverse-dependencies
.PHONY: triage-issue
.PHONY: backport-fix
.PHONY:
.PHONY: # Development and testing
.PHONY: check check-find-package-dependents-script

## Defaults
COMPOSE ?= podman compose

check-jira-tickets: PROJECT ?= RHEL
check-jira-tickets: COMPONENT ?= cockpit

issue-details: ISSUE ?= RHEL-78418

rebase-package: PACKAGE ?= cockpit
rebase-package: VERSION ?= 339
rebase-package: JIRA_ISSUES ?= "RHEL-123"

reverse-dependencies: ARCH ?= x86_64
reverse-dependencies: PACKAGE ?= podman

test-package: PACKAGE ?= podman
test-package: DIST_GIT_BRANCH ?= c10s
test-package: GIT_URL ?= https://gitlab.com/redhat/centos-stream/rpms
test-package: RPM_COMPOSE ?= CentOS-Stream-10

test-reverse-dependencies: ARCH ?= x86_64
test-reverse-dependencies: PACKAGE ?= podman
test-reverse-dependencies: CHANGE ?=
test-reverse-dependencies: DIST_GIT_BRANCH ?= c10s
test-reverse-dependencies: GIT_URL ?= https://gitlab.com/redhat/centos-stream/rpms
test-reverse-dependencies: RPM_COMPOSE ?= CentOS-Stream-10

triage-issue: ISSUE ?= RHEL-78418

## Operations
build:
	$(COMPOSE) build

debug-build:
	BUILD_TARGET=debug $(COMPOSE) build

run-mcp-atlassian:
	$(COMPOSE) up -d mcp-atlassian

stop-mcp-atlassian:
	$(COMPOSE) down mcp-atlassian

logs-mcp-atlassian:
	$(COMPOSE) logs -f mcp-atlassian

run-mcp-testing-farm:
	$(COMPOSE) up -d mcp-testing-farm

stop-mcp-testing-farm:
	$(COMPOSE) down mcp-testing-farm

logs-mcp-testing-farm:
	$(COMPOSE) logs -f mcp-testing-farm

run-goose:
	- $(COMPOSE) up -d goose
	$(COMPOSE) exec goose goose

run-goose-bash:
	- $(COMPOSE) up -d goose
	$(COMPOSE) exec goose bash

check-jira-tickets:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/check-jira-tickets.yaml \
			--params project=$(PROJECT) \
			--params component=$(COMPONENT)"

issue-details:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/issue-details.yaml \
			--params issue=$(ISSUE)"

triage-issue:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/triage-issue.yaml \
			--params issue=$(ISSUE)"

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
			/usr/local/bin/goose run --recipe recipes/rebase-package.yaml \
			--params package=$(PACKAGE) \
			--params version=$(VERSION) \
			--params jira_issues=$(JIRA_ISSUES) \
			--params gitlab_user=$(GITLAB_USER) && echo 'Recipe completed. Dropping into shell...' && /bin/bash"

backport-fix:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "set -e; \
			set +x; \
			askpass=\"\$$(mktemp)\"; \
			echo '#!/bin/sh' > \"\$$askpass\"; \
			echo 'echo \$$GITLAB_TOKEN' >> \"\$$askpass\"; \
			chmod +x \"\$$askpass\"; \
			export GIT_ASKPASS=\"\$$askpass\"; \
			/usr/local/bin/goose run --recipe recipes/backport-fix.yaml \
			--params package=$(PACKAGE) \
			--params upstream_fix=$(BACKPORT_FIX) \
			--params jira_issue=$(JIRA_ISSUE) \
			--params gitlab_user=$(GITLAB_USER) && echo 'Recipe completed. Dropping into shell...' && /bin/bash"

reverse-dependencies:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/reverse-dependencies.yaml \
			--params arch=$(ARCH) \
			--params package=$(PACKAGE)"

test-package:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/test-package.yaml \
			--params git_url=$(GIT_URL) \
			--params package=$(PACKAGE) \
			--params dist_git_branch=$(DIST_GIT_BRANCH) \
			--params compose=$(RPM_COMPOSE)"

test-reverse-dependencies:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/test-reverse-dependencies.yaml \
			--params arch=$(ARCH) \
			--params package=$(PACKAGE) \
			--params change='$(CHANGE)' \
			--params git_url=$(GIT_URL) \
			--params dist_git_branch=$(DIST_GIT_BRANCH) \
			--params compose=$(RPM_COMPOSE)"

config: GLOBAL_TEMPLATE = templates/compose.env
config: SECRET_TEMPLATES = $(filter-out $(GLOBAL_TEMPLATE), $(wildcard templates/*))
config:
	mkdir -p .secrets
	cp -n $(SECRET_TEMPLATES) .secrets/
	cp -n $(GLOBAL_TEMPLATE) .env

clean:
	$(COMPOSE) down
	podman volume prune -f

check: check-find-package-dependents-script

check-find-package-dependents-script:
	cd scripts && python tests/test-find-package-dependents.py

help:
	@echo "Available targets:"
	@echo "  config                      - Copy config templates to .secrets/ and .env"
	@echo "  build                       - Build all images"
	@echo "  debug-build                 - Build all images and rebuild goose from source"
	@echo "  run-mcp-atlassian           - Start Atlassian MCP server in background"
	@echo "  stop-mcp-atlassian          - Stop Atlassian MCP server"
	@echo "  logs-mcp-atlassian          - Show Atlassian MCP server logs"
	@echo "  run-mcp-testing-farm        - Start testing-farm MCP server in background"
	@echo "  stop-mcp-testing-farm       - Stop testing-farm MCP server"
	@echo "  logs-mcp-testing-farm       - Show testing-farm MCP server logs"
	@echo "  run-goose                   - Run goose interactively"
	@echo "  run-goose-bash              - Run goose with bash shell"
	@echo "  test-package                - Submit package testing request to testing farm"
	@echo "  test-reverse-dependencies   - Test all reverse dependencies of a package"
	@echo "  <recipe>                    - To run the recipes/<recipe>.yaml"
	@echo "  check                       - Run all development tests"
	@echo "  clean                       - Stop all services and clean volumes"
