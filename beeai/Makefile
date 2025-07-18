IMAGE_NAME ?= beeai-agent
COMPOSE_FILE ?= compose.yaml

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

.PHONY: start-valkey
start-valkey:
	$(COMPOSE) -f $(COMPOSE_FILE) up -d valkey

.PHONY: stop-valkey
stop-valkey:
	$(COMPOSE) -f $(COMPOSE_FILE) stop valkey

.PHONY: start-phoenix
start-phoenix:
	$(COMPOSE) -f $(COMPOSE_FILE) up -d phoenix

.PHONY: stop-phoenix
stop-phoenix:
	$(COMPOSE) -f $(COMPOSE_FILE) stop phoenix

.PHONY: run-beeai-bash
run-beeai-bash:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm beeai-agent /bin/bash

.PHONY: run-triage-agent-standalone
run-triage-agent-standalone:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		beeai-agent /usr/bin/python agents/triage_agent.py

.PHONY: run-triage-agent
run-triage-agent:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		beeai-agent /usr/bin/python agents/triage_agent.py

.PHONY: run-triage-agent-detached
run-triage-agent-detached:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm --detach \
		beeai-agent /usr/bin/python agents/triage_agent.py

.PHONY: run-rebase-agent-standalone
run-rebase-agent-standalone:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e VERSION=$(VERSION) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		beeai-agent /usr/bin/python agents/rebase_agent.py

.PHONY: run-rebase-agent
run-rebase-agent:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		beeai-agent /usr/bin/python agents/rebase_agent.py

.PHONY: run-rebase-agent-detached
run-rebase-agent-detached:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm --detach \
		beeai-agent /usr/bin/python agents/rebase_agent.py

.PHONY: run-backport-agent-standalone
run-backport-agent-standalone:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		-e PACKAGE=$(PACKAGE) \
		-e UPSTREAM_FIX=$(UPSTREAM_FIX) \
		-e JIRA_ISSUE=$(JIRA_ISSUE) \
		-e BRANCH=$(BRANCH) \
		beeai-agent /usr/bin/python agents/backport_agent.py

.PHONY: run-backport-agent
run-backport-agent:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm \
		beeai-agent /usr/bin/python agents/backport_agent.py

.PHONY: run-backport-agent-detached
run-backport-agent-detached:
	$(COMPOSE) -f $(COMPOSE_FILE) run --rm --detach \
		beeai-agent /usr/bin/python agents/backport_agent.py

.PHONY: clean
clean:
	$(COMPOSE) -f $(COMPOSE_FILE) down --volumes
