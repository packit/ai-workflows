#!/bin/sh

set -e

oc project jotnar-ymir--jotnar-ymir

apply() {
    echo "Applying $1 ..."
    oc apply -n jotnar-ymir--jotnar-ymir -f "$1"
}

import_image() {
    echo "Importing image $1 ..."
    oc import-image "$1" --all -n jotnar-ymir--jotnar-ymir
}

# Shared ConfigMaps
apply configmap-agents-env.yml
apply configmap-chat-env.yml
apply configmap-endpoints-env.yml
apply configmap-jira-env.yml
apply configmap-kerberos-env.yml

# # Phoenix (observability)
# # TODO: image quay.io/antbob/jotnar/phoenix is private — skipping until access is sorted
# apply imagestream-phoenix.yml
# import_image phoenix
# apply pvc-phoenix-data.yml
# apply service-phoenix.yml
# # TODO: route-phoenix.yml skipped — admission webhook panics on this cluster (platform bug)
# # Create manually: oc create route edge phoenix --service=phoenix --port=6006-tcp --insecure-policy=Redirect -n jotnar-ymir--jotnar-ymir
# # apply route-phoenix.yml
# apply deployment-phoenix.yml

# Valkey
apply imagestream-valkey.yml
import_image valkey
apply pvc-valkey-data.yml
apply service-valkey.yml
apply deployment-valkey.yml

# # Redis Commander
# apply imagestream-redis-commander.yml
# import_image redis-commander
# apply service-redis-commander.yml
# apply deployment-redis-commander.yml

# MCP Server
apply imagestream-mcp-gateway.yml
import_image mcp-server
apply pvc-mcp-server-git-repos.yml
apply service-mcp-gateway.yml
apply deployment-mcp-gateway.yml

# BeeAI Agents
apply imagestream-beeai-agent.yml
import_image beeai-agent
apply deployment-triage-agent.yml
# apply deployment-backport-agent-c9s.yml
apply deployment-backport-agent-c10s.yml
# apply deployment-rebase-agent-c9s.yml
# apply deployment-rebase-agent-c10s.yml
#
# # Supervisor
# apply imagestream-supervisor.yml
# import_image supervisor
# apply deployment-supervisor-processor.yml
# apply cronjob-supervisor-collector.yml
#
# # Jira Issue Fetcher
# apply imagestream-jira-issue-fetcher.yml
# import_image jira-issue-fetcher
# apply configmap-jira-issue-fetcher-env.yml
# apply cronjob-jira-issue-fetcher.yml
