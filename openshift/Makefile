# Run recipes under bash with pipefail so a failing `oc exec` in an
# `oc ... | jq | tac` pipeline aborts the target instead of being masked by the
# exit status of the trailing command (which would look like an empty queue).
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

# Reverse to oldest-first (next-to-pop) order. tac is GNU-only; fall back to
# `tail -r` on macOS/BSD where tac is absent.
TAC := $(shell command -v tac 2>/dev/null || echo "tail -r")

# Dump an input queue and its ymir_todo priority twin (drained first by the
# agents' BRPOP), oldest-first to match pop order.
define show_queue
@echo "=== $(1)_todo (ymir_todo, priority — popped first) ==="
@oc exec deployment/valkey -- valkey-cli --raw LRANGE $(1)_todo 0 -1 | jq -r '.metadata.issue // empty' | $(TAC)
@echo
@echo "=== $(1) (normal) ==="
@oc exec deployment/valkey -- valkey-cli --raw LRANGE $(1) 0 -1 | jq -r '.metadata.issue // empty' | $(TAC)
endef

# Dump a single input queue (one with no priority twin), oldest-first.
define show_single_queue
@echo "=== $(1) ==="
@oc exec deployment/valkey -- valkey-cli --raw LRANGE $(1) 0 -1 | jq -r '.metadata.issue // empty' | $(TAC)
endef

deploy:
	./deploy.sh

run-jira-issue-fetcher:
	oc delete job jira-issue-fetcher-manual || true
	oc create job jira-issue-fetcher-manual --from=cronjob/jira-issue-fetcher

run-jira-issue-fetcher-todo:
	oc delete job jira-issue-fetcher-todo-manual || true
	oc create job jira-issue-fetcher-todo-manual --from=cronjob/jira-issue-fetcher-todo

suspend-jira-issue-fetcher:
	oc patch cronjob jira-issue-fetcher --type merge -p '{"spec":{"suspend":true}}'

unsuspend-jira-issue-fetcher:
	oc patch cronjob jira-issue-fetcher --type merge -p '{"spec":{"suspend":false}}'

suspend-jira-issue-fetcher-todo:
	oc patch cronjob jira-issue-fetcher-todo --type merge -p '{"spec":{"suspend":true}}'

unsuspend-jira-issue-fetcher-todo:
	oc patch cronjob jira-issue-fetcher-todo --type merge -p '{"spec":{"suspend":false}}'

show-triage-queue:
	$(call show_queue,triage_queue)

show-rebase-queue-c9s:
	$(call show_queue,rebase_queue_c9s)

show-rebase-queue-c10s:
	$(call show_queue,rebase_queue_c10s)

show-backport-queue-c9s:
	$(call show_queue,backport_queue_c9s)

show-backport-queue-c10s:
	$(call show_queue,backport_queue_c10s)

show-rebuild-queue-c9s:
	$(call show_queue,rebuild_queue_c9s)

show-rebuild-queue-c10s:
	$(call show_queue,rebuild_queue_c10s)

show-clarification-queue:
	$(call show_single_queue,clarification_needed_queue)

show-error-list:
	@oc exec deployment/valkey -- valkey-cli --no-raw LRANGE error_list 0 -1 | python3 scripts/error_list.py --file -

logs-triage:
	oc logs -f deployment/triage-agent

logs-backport-c9s:
	oc logs -f deployment/backport-agent-c9s

logs-backport-c10s:
	oc logs -f deployment/backport-agent-c10s

logs-rebase-c9s:
	oc logs -f deployment/rebase-agent-c9s

logs-rebase-c10s:
	oc logs -f deployment/rebase-agent-c10s

logs-rebuild-c9s:
	oc logs -f deployment/rebuild-agent-c9s

logs-rebuild-c10s:
	oc logs -f deployment/rebuild-agent-c10s

logs-mcp:
	oc logs -f deployment/mcp-gateway

logs-supervisor:
	oc logs -f deployment/supervisor-processor

logs-valkey:
	oc logs -f deployment/valkey

logs-phoenix:
	oc logs -f deployment/phoenix

logs-redis-commander:
	oc logs -f deployment/redis-commander

logs-otel-collector:
	oc logs -f deployment/otel-collector

.PHONY: deploy run-jira-issue-fetcher run-jira-issue-fetcher-todo \
	suspend-jira-issue-fetcher unsuspend-jira-issue-fetcher \
	suspend-jira-issue-fetcher-todo unsuspend-jira-issue-fetcher-todo \
	show-triage-queue show-rebase-queue-c9s show-rebase-queue-c10s \
	show-backport-queue-c9s show-backport-queue-c10s \
	show-rebuild-queue-c9s show-rebuild-queue-c10s \
	show-clarification-queue show-error-list \
	logs-triage logs-backport-c9s logs-backport-c10s \
	logs-rebase-c9s logs-rebase-c10s logs-rebuild-c9s logs-rebuild-c10s \
	logs-mcp logs-supervisor logs-valkey logs-phoenix \
	logs-redis-commander logs-otel-collector
