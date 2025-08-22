IMAGE_NAME ?= beeai-agent
COMPOSE_FILE ?= compose.yaml
DRY_RUN ?= false

COMPOSE ?= $(shell command -v podman >/dev/null 2>&1 && echo "podman compose" || echo "docker-compose")
COMPOSE_AGENTS=$(COMPOSE) -f $(COMPOSE_FILE) --profile=agents
COMPOSE_SUPERVISOR=$(COMPOSE) -f $(COMPOSE_FILE) --profile=supervisor

.PHONY: build
build: certs/Current-IT-Root-CAs.pem
	$(COMPOSE) -f $(COMPOSE_FILE) build

certs/Current-IT-Root-CAs.pem:
	curl -o certs/Current-IT-Root-CAs.pem \
	https://certs.corp.redhat.com/certs/Current-IT-Root-CAs.pem

.PHONY: run-beeai-bash
run-beeai-bash:
	$(COMPOSE_AGENTS) run --rm triage-agent /bin/bash

.PHONY: run-triage-agent-standalone
run-triage-agent-standalone:
	$(COMPOSE_AGENTS) run --rm \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e DRY_RUN=$(DRY_RUN) \
		triage-agent




.PHONY: run-rebase-agent-standalone
run-rebase-agent-standalone:
	$(COMPOSE_AGENTS) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e VERSION=$(VERSION) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		-e DRY_RUN=$(DRY_RUN) \
		rebase-agent





.PHONY: run-backport-agent-standalone
run-backport-agent-standalone:
	$(COMPOSE_AGENTS) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e UPSTREAM_FIX=$(UPSTREAM_FIX) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		-e DRY_RUN=$(DRY_RUN) \
		-e CVE_ID=$(CVE_ID) \
		backport-agent


# Essential 3-Agent Architecture Targets

.PHONY: start
start:
	DRY_RUN=$(DRY_RUN) $(COMPOSE_AGENTS) up

.PHONY: start-detached
start-detached:
	DRY_RUN=$(DRY_RUN) $(COMPOSE_AGENTS) up -d

.PHONY: stop
stop:
	$(COMPOSE) -f $(COMPOSE_FILE) down

.PHONY: clean
clean:
	$(COMPOSE) -f $(COMPOSE_FILE) down --volumes


.PHONY: logs-triage
logs-triage:
	$(COMPOSE_AGENTS) logs -f triage-agent

.PHONY: logs-backport
logs-backport:
	$(COMPOSE_AGENTS) logs -f backport-agent

.PHONY: logs-rebase
logs-rebase:
	$(COMPOSE_AGENTS) logs -f rebase-agent

.PHONY: trigger-pipeline
trigger-pipeline:
	@if [ -z "$(JIRA_ISSUE)" ]; then \
		echo "Usage: make trigger-pipeline JIRA_ISSUE=RHEL-12345"; \
		exit 1; \
	fi
	@echo "Triggering pipeline for issue: $(JIRA_ISSUE)"
	$(COMPOSE_AGENTS) exec valkey redis-cli LPUSH triage_queue '{"metadata": {"issue": "$(JIRA_ISSUE)"}}'


# Testing and Release Supervisor

.PHONY: supervisor-start
supervisor-start:
	DRY_RUN=$(DRY_RUN) $(COMPOSE_SUPERVISOR) up

.PHONY: supervisor-start-detached
supervisor-start-detached:
	DRY_RUN=$(DRY_RUN) $(COMPOSE_SUPERVISOR) up -d

.PHONY: supervisor-stop
supervisor-stop:
	$(COMPOSE_SUPERVISOR) down

.PHONY: supervisor-clean
supervisor-clean:
	$(COMPOSE) -f $(COMPOSE_FILE) down --volumes

.PHONY: process-issue
process-issue:
	$(COMPOSE_SUPERVISOR) run --rm \
		supervisor python -m supervisor.main process-issue $(JIRA_ISSUE)

.PHONY: process-erratum
process-erratum:
	$(COMPOSE_SUPERVISOR) run --rm \
		supervisor python -m supervisor.main process-erratum $(ERRATA_ID)


# Common utility targets

.PHONY: status
status:
	$(COMPOSE) -f $(COMPOSE_FILE) ps

.PHONY: redis-cli
redis-cli:
	$(COMPOSE_AGENTS) exec valkey redis-cli


.PHONY: build-test-image
build-test-image:
	$(MAKE) -f Makefile.tests build-test-image

.PHONY: check-in-container
check-in-container:
	$(MAKE) -f Makefile.tests check-in-container
