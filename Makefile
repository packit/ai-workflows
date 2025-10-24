IMAGE_NAME ?= beeai-agent
COMPOSE_FILE ?= compose.yaml
DRY_RUN ?= false
LOKI_URL ?= http://loki.tft.osci.redhat.com/
LOKI_SINCE ?= 24h
LOKI_LIMIT ?= 3000
LOKI_POD ?= mcp-gateway

COMPOSE ?= $(shell command -v podman >/dev/null 2>&1 && echo "podman compose" || echo "docker-compose")
COMPOSE_AGENTS=$(COMPOSE) -f $(COMPOSE_FILE) --profile=agents
COMPOSE_SUPERVISOR=$(COMPOSE) -f $(COMPOSE_FILE) --profile=supervisor

.PHONY: build
build:
	$(COMPOSE) -f $(COMPOSE_FILE) --profile=agents --profile=supervisor build

.PHONY: run-beeai-bash
run-beeai-bash:
	$(COMPOSE_AGENTS) run --rm triage-agent /bin/bash

.PHONY: run-triage-agent-standalone
run-triage-agent-standalone:
	$(COMPOSE_AGENTS) run --rm \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e DRY_RUN=$(DRY_RUN) \
		triage-agent




.PHONY: run-rebase-agent-c9s-standalone
run-rebase-agent-c9s-standalone:
	$(COMPOSE_AGENTS) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e VERSION=$(VERSION) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		-e DRY_RUN=$(DRY_RUN) \
		rebase-agent-c9s

.PHONY: run-rebase-agent-c10s-standalone
run-rebase-agent-c10s-standalone:
	$(COMPOSE_AGENTS) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e VERSION=$(VERSION) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		-e DRY_RUN=$(DRY_RUN) \
		rebase-agent-c10s

.PHONY: run-rebase-agent-standalone
run-rebase-agent-standalone: run-rebase-agent-c10s-standalone





.PHONY: run-backport-agent-c9s-standalone
run-backport-agent-c9s-standalone:
	$(COMPOSE_AGENTS)  run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e UPSTREAM_FIX=$(UPSTREAM_FIX) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		-e DRY_RUN=$(DRY_RUN) \
		-e CVE_ID=$(CVE_ID) \
		backport-agent-c9s

.PHONY: run-backport-agent-c10s-standalone
run-backport-agent-c10s-standalone:
	$(COMPOSE_AGENTS) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e UPSTREAM_FIX=$(UPSTREAM_FIX) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		-e DRY_RUN=$(DRY_RUN) \
		-e CVE_ID=$(CVE_ID) \
		backport-agent-c10s

.PHONY: run-backport-agent-standalone
run-backport-agent-standalone: run-backport-agent-c10s-standalone

.PHONY: run-jira-issue-fetcher
run-jira-issue-fetcher:
	@echo "Running Jira Issue Fetcher..."
	@if [ ! -f .secrets/jira-issue-fetcher.env ]; then \
		echo "Error: .secrets/jira-issue-fetcher.env not found"; \
		echo "Copy the template: cp templates/jira-issue-fetcher.env .secrets/jira-issue-fetcher.env"; \
		echo "Then edit it with your credentials"; \
		exit 1; \
	fi
	@echo "Ensuring Redis is available (don't use depends_on otherwise it will kill agents already running)..."
	@$(COMPOSE) -f $(COMPOSE_FILE) up -d valkey || true  # Don't fail if valkey is already running
	@echo "Running jira-issue-fetcher..."
	$(COMPOSE) -f $(COMPOSE_FILE) --profile manual run --rm jira-issue-fetcher

.PHONY: build-jira-issue-fetcher
build-jira-issue-fetcher:
	$(COMPOSE) --profile manual build jira-issue-fetcher




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

.PHONY: logs-jira-issue-fetcher
logs-jira-issue-fetcher:
	$(COMPOSE) -f $(COMPOSE_FILE) --profile manual logs -f jira-issue-fetcher


# Loki logcli targets
# https://grafana.com/docs/loki/latest/setup/install/local/
# add their RPM repos and `dnf install logcli`
LOKI_CMD = logcli --output-timestamp-format=unixdate --addr=$(LOKI_URL) query --since=$(LOKI_SINCE) --limit=$(LOKI_LIMIT) -o raw
LOKI_FILTER = '{kubernetes_container_name="$(1)",kubernetes_namespace_name="jotnar-prod"} | json | line_format "{{._timestamp}} {{.kubernetes_pod_name}} {{.message}}"'

.PHONY: logs-loki-help
logs-loki-help:
	@echo "Available pod names:"
	@echo "  - triage-agent"
	@echo "  - backport-agent-c9s"
	@echo "  - backport-agent-c10s"
	@echo "  - rebase-agent-c9s"
	@echo "  - rebase-agent-c10s"
	@echo "  - supervisor-collector"
	@echo "  - supervisor-processor"
	@echo "  - mcp-gateway"
	@echo "  - valkey"
	@echo "Usage example: LOKI_SINCE=24h LOKI_POD=<pod-name> make logs-loki"

.PHONY: logs-loki
logs-loki:
	$(LOKI_CMD) $(call LOKI_FILTER,$(LOKI_POD))

.PHONY: trigger-pipeline
trigger-pipeline:
	@if [ -z "$(JIRA_ISSUE)" ]; then \
		echo "Usage: make trigger-pipeline JIRA_ISSUE=RHEL-12345"; \
		exit 1; \
	fi
	@echo "Triggering pipeline for issue: $(JIRA_ISSUE)"
	$(COMPOSE_AGENTS) exec valkey redis-cli LPUSH triage_queue '{"metadata": {"issue": "$(JIRA_ISSUE)"}}'


# Testing and Release Supervisor

DEBUG_LOWER := $(shell echo $(DEBUG) | tr '[:upper:]' '[:lower:]')
ifeq ($(DEBUG_LOWER),true)
DEBUG_FLAG := --debug
else
DEBUG_FLAG :=
endif

DRY_RUN_LOWER := $(shell echo $(DRY_RUN) | tr '[:upper:]' '[:lower:]')
ifeq ($(DRY_RUN_LOWER),true)
DRY_RUN_FLAG := --dry-run
else
DRY_RUN_FLAG :=
endif

IGNORE_NEEDS_ATTENTION_LOWER := $(shell echo $(IGNORE_NEEDS_ATTENTION) | tr '[:upper:]' '[:lower:]')
ifeq ($(IGNORE_NEEDS_ATTENTION_LOWER),true)
IGNORE_NEEDS_ATTENTION_FLAG := --ignore-needs-attention
else
IGNORE_NEEDS_ATTENTION_FLAG :=
endif

.PHONY: supervisor-clear-queue
supervisor-clear-queue:
	$(COMPOSE_SUPERVISOR) run --rm \
		supervisor python -m supervisor.main $(DEBUG_FLAG) clear-queue

.PHONY: supervisor-collect
supervisor-collect:
	$(COMPOSE_SUPERVISOR) run --rm \
		supervisor python -m supervisor.main $(DEBUG_FLAG) collect --no-repeat

.PHONY: process-issue
process-issue:
	$(COMPOSE_SUPERVISOR) run --rm \
		supervisor python -m supervisor.main $(DEBUG_FLAG) $(IGNORE_NEEDS_ATTENTION_FLAG) $(DRY_RUN_FLAG) process-issue $(JIRA_ISSUE)

.PHONY: process-erratum
process-erratum:
	$(COMPOSE_SUPERVISOR) run --rm \
		supervisor python -m supervisor.main $(DEBUG_FLAG) $(IGNORE_NEEDS_ATTENTION_FLAG) $(DRY_RUN_FLAG) process-erratum $(ERRATA_ID)


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

.PHONY: check-in-container check-agents-in-container check-mcp-server-in-container check-common-in-container
check-in-container:
	$(MAKE) -f Makefile.tests check-in-container
check-agents-in-container:
	$(MAKE) -f Makefile.tests check-agents-in-container
check-mcp-server-in-container:
	$(MAKE) -f Makefile.tests check-mcp-server-in-container
check-jira-issue-fetcher-in-container:
	$(MAKE) -f Makefile.tests check-jira-issue-fetcher-in-container
check-common-in-container:
	$(MAKE) -f Makefile.tests check-common-in-container
