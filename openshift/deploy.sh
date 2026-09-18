#!/bin/sh

set -e

oc project jotnar-ymir--jotnar-ymir

apply() {
    echo "Applying $1 ..."
    oc apply -n jotnar-ymir--jotnar-ymir -f "$1"
}

import_image() {
    local image=$1
    local max_retries=5
    local retry=0
    local delay=2

    echo "Importing image $image ..."

    while [ $retry -lt $max_retries ]; do
        if oc import-image "$image" --all; then
            return 0
        fi

        retry=$((retry + 1))
        if [ $retry -lt $max_retries ]; then
            echo "Import failed, retrying in ${delay}s... (attempt $retry/$max_retries)"
            sleep "$delay"
            delay=$((delay * 2))
        fi
    done

    echo "Failed to import image $image after $max_retries attempts"
    return 1
}

patch_route_tls_from_secret() {
    local route=$1
    local secret=$2

    if ! oc get secret "$secret" -o name >/dev/null 2>&1; then
        echo "WARNING: TLS secret '$secret' not found — skipping cert patch for route '$route'"
        return 0
    fi

    echo "Patching route $route with TLS cert/key from secret $secret ..."
    local cert key
    cert=$(oc get secret "$secret" -o jsonpath='{.data.tls\.crt}' | base64 -d)
    key=$(oc get secret "$secret" -o jsonpath='{.data.tls\.key}' | base64 -d)

    oc patch route "$route" --type merge -p "$(
        python3 -c "
import json, sys
print(json.dumps({'spec':{'tls':{
    'certificate': sys.argv[1],
    'key': sys.argv[2],
}}}))
" "$cert" "$key"
    )"
}

# Egress rules
apply tenant-egress.yml

# Shared ConfigMaps
apply configmap-agents-env.yml
apply configmap-chat-env.yml
apply configmap-endpoints-env.yml
apply configmap-jira-env.yml
apply configmap-kerberos-env.yml

# Phoenix PostgreSQL database
apply imagestream-phoenix-db.yml
import_image phoenix-db
apply pvc-phoenix-db-data.yml
apply service-phoenix-db.yml
apply deployment-phoenix-db.yml

# Phoenix (observability)
apply imagestream-phoenix.yml
import_image phoenix
apply pvc-phoenix-data.yml
apply service-phoenix.yml
apply route-phoenix.yml
apply deployment-phoenix.yml

# OTel Collector + Trace Server
apply imagestream-trace-server.yml
import_image trace-server
apply configmap-otel-collector-config.yml
apply configmap-trace-server-oidc-env.yml
apply pvc-trace-server-data.yml
apply service-otel-collector.yml
apply route-trace-server.yml
apply route-trace-server-cname.yml
patch_route_tls_from_secret trace-server-cname ymir-cname-tls
apply deployment-otel-collector.yml

# Valkey
apply imagestream-valkey.yml
import_image valkey
apply pvc-valkey-data.yml
apply service-valkey.yml
apply deployment-valkey.yml

# Redis Commander
apply imagestream-redis-commander.yml
import_image redis-commander
apply service-redis-commander.yml
apply route-redis-commander.yml
apply deployment-redis-commander.yml

# MCP Server
apply imagestream-mcp-gateway.yml
import_image mcp-server
apply pvc-mcp-server-git-repos.yml
apply service-mcp-gateway.yml
apply deployment-mcp-gateway.yml

# API
apply imagestream-api.yml
import_image ymir-api
apply configmap-api-oidc-env.yml
apply service-api.yml
apply route-api.yml
apply deployment-api.yml

# BeeAI Agents
apply imagestream-beeai-agent.yml
import_image beeai-agent
apply deployment-triage-agent.yml
apply deployment-backport-agent-c9s.yml
apply deployment-backport-agent-c10s.yml
apply deployment-rebase-agent-c9s.yml
apply deployment-rebase-agent-c10s.yml
apply deployment-rebuild-agent-c9s.yml
apply deployment-rebuild-agent-c10s.yml
apply deployment-reproducer-agent.yml
apply deployment-mr-consolidation-agent-c9s.yml
apply deployment-mr-consolidation-agent-c10s.yml

# Jira Issue Fetcher
apply imagestream-jira-issue-fetcher.yml
import_image jira-issue-fetcher
apply configmap-jira-issue-fetcher-env.yml
apply configmap-jira-issue-fetcher-filter-env.yml
apply configmap-jira-issue-fetcher-todo-env.yml
apply cronjob-jira-issue-fetcher.yml
apply cronjob-jira-issue-fetcher-todo.yml

# MR Cleanup
apply imagestream-mr-cleanup.yml
import_image mr-cleanup
apply cronjob-mr-cleanup.yml

# Postponed Issue Sweeps
apply imagestream-sweep.yml
import_image sweep
apply cronjob-sweep-dependency.yml
apply cronjob-sweep-y-stream.yml
apply cronjob-sweep-pr-pending.yml
apply cronjob-sweep-no-patch.yml

# # Supervisor
# apply imagestream-supervisor.yml
# import_image supervisor
# apply deployment-supervisor-processor.yml
# apply cronjob-supervisor-collector.yml
