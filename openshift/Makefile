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
@oc exec deployment/valkey -- valkey-cli --raw LRANGE $(1)_todo 0 -1 | jq -r '(.metadata.jira_issue // .metadata.issue // empty) | if type=="object" then (.key // empty) else . end' | $(TAC)
@echo "length: $$(oc exec deployment/valkey -- valkey-cli --raw LLEN $(1)_todo)"
@echo
@echo "=== $(1) (normal) ==="
@oc exec deployment/valkey -- valkey-cli --raw LRANGE $(1) 0 -1 | jq -r '(.metadata.jira_issue // .metadata.issue // empty) | if type=="object" then (.key // empty) else . end' | $(TAC)
@echo "length: $$(oc exec deployment/valkey -- valkey-cli --raw LLEN $(1))"
endef

# Dump a single input queue (one with no priority twin), oldest-first.
define show_single_queue
@echo "=== $(1) ==="
@oc exec deployment/valkey -- valkey-cli --raw LRANGE $(1) 0 -1 | jq -r '(.metadata.jira_issue // .metadata.issue // empty) | if type=="object" then (.key // empty) else . end' | $(TAC)
@echo "length: $$(oc exec deployment/valkey -- valkey-cli --raw LLEN $(1))"
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

show-reproducer-queue:
	$(call show_queue,reproducer_queue)

show-error-list:
	@oc exec deployment/valkey -- valkey-cli --no-raw LRANGE error_list 0 -1 | python3 scripts/error_list.py --file -

# Requeue a specific error_list entry (by the [id=...] shown in show-error-list)
# back onto its original trigger queue, with attempts reset to 0.
# Required: ERROR_ID
requeue-error:
	@if [ -z "$(ERROR_ID)" ]; then \
		echo "Usage: make requeue-error ERROR_ID=42"; \
		exit 1; \
	fi
	python3 scripts/requeue_error.py $(ERROR_ID)

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

logs-reproducer:
	oc logs -f deployment/reproducer-agent

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

process:
	@if [ -z "$(ISSUE)" ]; then \
		echo "ERROR: ISSUE parameter is required. Usage: make process ISSUE=RHEL-XXXX"; \
		exit 1; \
	fi
	@echo "Pushing $(ISSUE) to triage_queue..."
	@oc exec deployment/valkey -- valkey-cli LPUSH triage_queue \
		'{"metadata":{"issue":"$(ISSUE)"},"attempts":0,"user_triggered":false}'
	@echo "✓ Task queued successfully. View queue with: make show-triage-queue"

# Manually enqueue a reproducer job (also when TRIAGE_ENQUEUE_REPRODUCER=false).
# Same interface as the local `make trigger-reproducer` target.
# Required: JIRA_ISSUE, PACKAGE
# Optional: CVE_ID, FIX_VERSION, TARGET_BRANCH, TRIAGE_SUMMARY, USER_TRIGGERED=true
trigger-reproducer:
	@if [ -z "$(JIRA_ISSUE)" ] || [ -z "$(PACKAGE)" ]; then \
		echo "Usage: make trigger-reproducer JIRA_ISSUE=RHEL-12345 PACKAGE=bind \\"; \
		echo "         [CVE_ID=CVE-2025-12345] [FIX_VERSION=rhel-10.1] [TARGET_BRANCH=c10s] \\"; \
		echo "         [TRIAGE_SUMMARY='…'] [USER_TRIGGERED=true]"; \
		exit 1; \
	fi
	@echo "Enqueueing reproducer for $(JIRA_ISSUE) (package=$(PACKAGE), user_triggered=$(or $(USER_TRIGGERED),false))"
	@payload=$$( \
		JIRA_ISSUE="$(JIRA_ISSUE)" \
		PACKAGE="$(PACKAGE)" \
		CVE_ID="$(CVE_ID)" \
		FIX_VERSION="$(FIX_VERSION)" \
		TARGET_BRANCH="$(TARGET_BRANCH)" \
		TRIAGE_SUMMARY="$(TRIAGE_SUMMARY)" \
		USER_TRIGGERED="$(or $(USER_TRIGGERED),false)" \
		python3 -c 'import json, os; \
meta = {"jira_issue": os.environ["JIRA_ISSUE"], "package": os.environ["PACKAGE"]}; \
[meta.__setitem__(k, v) for k, e in ( \
    ("cve_id", "CVE_ID"), \
    ("fix_version", "FIX_VERSION"), \
    ("target_branch", "TARGET_BRANCH"), \
    ("triage_summary", "TRIAGE_SUMMARY"), \
) if (v := (os.environ.get(e) or "").strip())]; \
print(json.dumps({ \
    "metadata": meta, \
    "attempts": 0, \
    "user_triggered": os.environ.get("USER_TRIGGERED", "false").lower() == "true", \
}))' \
	) && \
	queue=$$(if [ "$(or $(USER_TRIGGERED),false)" = "true" ]; then echo reproducer_queue_todo; else echo reproducer_queue; fi) && \
	oc exec deployment/valkey -- valkey-cli LPUSH $$queue "$$payload" && \
	echo "✓ Pushed to $$queue. View with: make show-reproducer-queue"

run-sweep-dependency:
	oc delete job sweep-dependency-manual --ignore-not-found
	oc create job sweep-dependency-manual --from=cronjob/sweep-dependency

run-sweep-y-stream:
	oc delete job sweep-y-stream-manual --ignore-not-found
	oc create job sweep-y-stream-manual --from=cronjob/sweep-y-stream

run-sweep-pr-pending:
	oc delete job sweep-pr-pending-manual --ignore-not-found
	oc create job sweep-pr-pending-manual --from=cronjob/sweep-pr-pending

run-sweep-no-patch:
	oc delete job sweep-no-patch-manual --ignore-not-found
	oc create job sweep-no-patch-manual --from=cronjob/sweep-no-patch

suspend-sweeps:
	oc patch cronjob sweep-dependency --type merge -p '{"spec":{"suspend":true}}'
	oc patch cronjob sweep-y-stream --type merge -p '{"spec":{"suspend":true}}'
	oc patch cronjob sweep-pr-pending --type merge -p '{"spec":{"suspend":true}}'
	oc patch cronjob sweep-no-patch --type merge -p '{"spec":{"suspend":true}}'

unsuspend-sweeps:
	oc patch cronjob sweep-dependency --type merge -p '{"spec":{"suspend":false}}'
	oc patch cronjob sweep-y-stream --type merge -p '{"spec":{"suspend":false}}'
	oc patch cronjob sweep-pr-pending --type merge -p '{"spec":{"suspend":false}}'
	oc patch cronjob sweep-no-patch --type merge -p '{"spec":{"suspend":false}}'

.PHONY: deploy run-jira-issue-fetcher run-jira-issue-fetcher-todo \
	suspend-jira-issue-fetcher unsuspend-jira-issue-fetcher \
	suspend-jira-issue-fetcher-todo unsuspend-jira-issue-fetcher-todo \
	show-triage-queue show-rebase-queue-c9s show-rebase-queue-c10s \
	show-backport-queue-c9s show-backport-queue-c10s \
	show-rebuild-queue-c9s show-rebuild-queue-c10s \
	show-clarification-queue show-reproducer-queue show-error-list requeue-error \
	logs-triage logs-backport-c9s logs-backport-c10s \
	logs-rebase-c9s logs-rebase-c10s logs-rebuild-c9s logs-rebuild-c10s \
	logs-reproducer logs-mcp logs-supervisor logs-valkey logs-phoenix \
	logs-redis-commander logs-otel-collector process trigger-reproducer \
	run-sweep-dependency run-sweep-y-stream run-sweep-pr-pending run-sweep-no-patch \
	suspend-sweeps unsuspend-sweeps
