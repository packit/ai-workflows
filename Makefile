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
DIST_GIT_BRANCH ?= "c10s"
.PHONY: rebase-package
rebase-package:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/rebase-package.yaml \
			--params package=$(PACKAGE) \
			--params version=$(VERSION) \
			--params dist_git_branch=$(DIST_GIT_BRANCH) \
			--params jira_issues=$(JIRA_ISSUES)"

PACKAGE ?= podman
.PHONY: reverse-dependencies
reverse-dependencies:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/usr/local/bin/goose run --recipe recipes/reverse-dependencies.yaml \
			--params package=$(PACKAGE)"

ISSUE ?= RHEL-78418
.PHONY: workflow
workflow:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "/home/goose/workflow.sh $(ISSUE)"

.PHONY: issue-details-ansible
issue-details-ansible:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "cd /home/goose/playbooks && PYTHONUNBUFFERED=1 ansible-playbook issue_details.yaml -e issue=$(ISSUE) -vvv"

.PHONY: rebase-package-ansible
rebase-package-ansible:
	$(COMPOSE) run --rm \
		--entrypoint /bin/sh goose \
		-c "cd /home/goose/playbooks && PYTHONUNBUFFERED=1 ansible-playbook rebase-package.yaml -vvv"


.PHONY: workflow-ansible
workflow-ansible: issue-details-ansible rebase-package-ansible

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
	@echo "  issue-details-ansible       - Run issue details analysis with Ansible"
	@echo "  workflow-ansible            - Run complete workflow with Ansible"
	@echo "  rebase-package-ansible      - Run package rebase with Ansible"
	@echo "  clean                       - Stop all services and clean volumes"
