#!/bin/sh

set -e

oc project jotnar-ymir--jotnar-ymir

apply() {
    echo "Applying $1 ..."
    oc apply -n jotnar-ymir--jotnar-ymir -f "$1"
}

# Shared ConfigMaps
apply configmap-agents-env.yml
apply configmap-chat-env.yml
apply configmap-endpoints-env.yml
apply configmap-jira-env.yml
apply configmap-kerberos-env.yml

# Phoenix (observability)
apply imagestream-phoenix.yml
apply pvc-phoenix-data.yml
apply service-phoenix.yml
apply route-phoenix.yml
apply deployment-phoenix.yml

# Valkey
apply imagestream-valkey.yml
apply pvc-valkey-data.yml
apply service-valkey.yml
apply deployment-valkey.yml

# Redis Commander
apply imagestream-redis-commander.yml
apply service-redis-commander.yml
apply deployment-redis-commander.yml

# MCP Server
apply imagestream-mcp-gateway.yml
apply pvc-mcp-server-git-repos.yml
apply service-mcp-gateway.yml
apply deployment-mcp-gateway.yml

# BeeAI Agents
apply imagestream-beeai-agent.yml
apply service-triage-agent.yml
apply service-backport-agent.yml
apply service-rebase-agent.yml
apply deployment-triage-agent.yml
apply deployment-backport-agent-c9s.yml
apply deployment-backport-agent-c10s.yml
apply deployment-rebase-agent-c9s.yml
apply deployment-rebase-agent-c10s.yml

# Supervisor
apply imagestream-supervisor.yml
apply deployment-supervisor-processor.yml
apply cronjob-supervisor-collector.yml

# Jira Issue Fetcher
apply imagestream-jira-issue-fetcher.yml
apply configmap-jira-issue-fetcher-env.yml
apply cronjob-jira-issue-fetcher.yml
