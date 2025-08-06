IMAGE_NAME ?= beeai-agent
COMPOSE_FILE ?= compose.yaml
DRY_RUN ?= false

COMPOSE ?= $(shell command -v podman >/dev/null 2>&1 && echo "podman compose" || echo "docker-compose")

.PHONY: build
build:
	$(COMPOSE) -f $(COMPOSE_FILE) build


.PHONY: run-beeai-bash
run-beeai-bash:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm triage-agent /bin/bash

.PHONY: run-mcp-gateway-bash
run-mcp-gateway-bash:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm mcp-gateway /bin/bash

.PHONY: run-triage-agent-standalone
run-triage-agent-standalone:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		triage-agent




.PHONY: run-rebase-agent-standalone
run-rebase-agent-standalone:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e VERSION=$(VERSION) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		-e DRY_RUN=$(DRY_RUN) \
		rebase-agent





.PHONY: run-backport-agent-standalone
run-backport-agent-standalone:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e UPSTREAM_FIX=$(UPSTREAM_FIX) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		-e DRY_RUN=$(DRY_RUN) \
		backport-agent





# Essential 3-Agent Architecture Targets

.PHONY: start
start:
	DRY_RUN=$(DRY_RUN) $(COMPOSE) -f $(COMPOSE_FILE) up

.PHONY: start-detached
start-detached:
	DRY_RUN=$(DRY_RUN) $(COMPOSE) -f $(COMPOSE_FILE) up -d

.PHONY: stop
stop:
	$(COMPOSE) -f $(COMPOSE_FILE) down



.PHONY: logs-triage
logs-triage:
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f triage-agent

.PHONY: logs-backport
logs-backport:
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f backport-agent

.PHONY: logs-rebase
logs-rebase:
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f rebase-agent

.PHONY: status
status:
	$(COMPOSE) -f $(COMPOSE_FILE) ps

.PHONY: trigger-pipeline
trigger-pipeline:
	@if [ -z "$(JIRA_ISSUE)" ]; then \
		echo "Usage: make trigger-pipeline JIRA_ISSUE=RHEL-12345"; \
		exit 1; \
	fi
	@echo "Triggering pipeline for issue: $(JIRA_ISSUE)"
	$(COMPOSE) -f $(COMPOSE_FILE) exec valkey redis-cli LPUSH triage_queue '{"metadata": {"issue": "$(JIRA_ISSUE)"}}'

.PHONY: redis-cli
redis-cli:
	$(COMPOSE) -f $(COMPOSE_FILE) exec valkey redis-cli


.PHONY: clean
clean:
	$(COMPOSE) -f $(COMPOSE_FILE) down --volumes


.PHONY: build-test-image
build-test-image:
	$(MAKE) -f Makefile.tests build-test-image

.PHONY: check-in-container
check-in-container:
	$(MAKE) -f Makefile.tests check-in-container
