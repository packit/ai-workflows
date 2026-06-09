deploy:
	./deploy.sh

run-jira-issue-fetcher:
	oc delete job jira-issue-fetcher-manual || true
	oc create job jira-issue-fetcher-manual --from=cronjob/jira-issue-fetcher

run-jira-issue-fetcher-todo:
	oc delete job jira-issue-fetcher-todo-manual || true
	oc create job jira-issue-fetcher-todo-manual --from=cronjob/jira-issue-fetcher-todo

show-triage-queue:
	@echo "=== triage_queue_todo (ymir_todo, priority — popped first) ==="
	@oc exec deployment/valkey -- valkey-cli LRANGE triage_queue_todo 0 -1 | jq -r .metadata.issue | tac
	@echo
	@echo "=== triage_queue (normal) ==="
	@oc exec deployment/valkey -- valkey-cli LRANGE triage_queue 0 -1 | jq -r .metadata.issue | tac

	oc exec deployment/valkey -- valkey-cli LRANGE triage_queue 0 -1 | jq -r .metadata.issue | tac

show-error-list:
	@oc exec deployment/valkey -- valkey-cli LRANGE error_list 0 -1 | python3 scripts/error_list.py --file -

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

.PHONY: deploy run-jira-issue-fetcher run-jira-issue-fetcher-todo \
	show-triage-queue show-error-list \
	logs-triage logs-backport-c9s logs-backport-c10s \
	logs-rebase-c9s logs-rebase-c10s logs-rebuild-c9s logs-rebuild-c10s \
	logs-mcp
