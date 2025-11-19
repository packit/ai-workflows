deploy:
	./deploy.sh

run-jira-issue-fetcher:
	oc delete job jira-issue-fetcher-manual || true
	oc create job jira-issue-fetcher-manual --from=cronjob/jira-issue-fetcher

run-jira-postponed-issue-fetcher:
	oc delete job jira-postponed-issue-fetcher-manual || true
	oc create job jira-postponed-issue-fetcher-manual --from=cronjob/jira-postponed-issue-fetcher

.PHONY: deploy run-jira-issue-fetcher run-jira-postponed-issue-fetcher
