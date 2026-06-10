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

.PHONY: deploy run-jira-issue-fetcher run-jira-issue-fetcher-todo show-triage-queue
