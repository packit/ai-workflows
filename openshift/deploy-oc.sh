#!/bin/sh

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

oc project jotnar-ymir--jotnar-ymir

apply() {
    echo "Applying $1 ..."
    oc apply -n jotnar-ymir--jotnar-ymir -f "$1"
}

import_image() {
    local image=$1
    local source=$2
    local max_retries=5
    local retry=0
    local delay=2

    echo "Importing image $image from $source ..."

    while [ $retry -lt $max_retries ]; do
        if oc import-image "$image" --from="$source" --confirm --scheduled=false; then
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
import_image phoenix-db:prod quay.io/sclorg/postgresql-16-c9s@sha256:c9990e93bf07321bf5f8cc31ef97951ec181d0f40293c0438422e8e53e2ef06e
apply pvc-phoenix-db-data.yml
apply service-phoenix-db.yml
apply deployment-phoenix-db.yml

# Phoenix (observability)
apply imagestream-phoenix.yml
import_image phoenix:prod quay.io/jotnar/phoenix@sha256:51f52f728be6684d48c7797fcf2eb3f9674e85b2b700d4e8628d27024f6f1653
apply pvc-phoenix-data.yml
apply service-phoenix.yml
apply route-phoenix.yml
apply deployment-phoenix.yml

# OTel Collector + Trace Server
apply imagestream-trace-server.yml
import_image trace-server:prod quay.io/jotnar/trace-server@sha256:be63c3df7efe41a6d3e3412ae6b16a7489427563986a9fe32abf7b660d122bff
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
import_image valkey:prod docker.io/valkey/valkey@sha256:0f62bfecc0b713dc2111af3ee307d5cd53c6877b7a112c6143c4de20f1325e4c
apply pvc-valkey-data.yml
apply service-valkey.yml
apply deployment-valkey.yml

# Redis Commander
apply imagestream-redis-commander.yml
import_image redis-commander:prod quay.io/jotnar/redis-commander@sha256:4143a32885b1ae81feea8080a423aab9127cdf4fd8cb13a1b8c2a48002159f97
apply service-redis-commander.yml
apply route-redis-commander.yml
apply deployment-redis-commander.yml

# MCP Server
apply imagestream-mcp-gateway.yml
import_image mcp-server:prod quay.io/jotnar/mcp-server@sha256:b8d351f304799b35aa20d812f418701a0a19f1a20621b9a697da93210a059457
apply pvc-mcp-server-git-repos.yml
apply service-mcp-gateway.yml
apply deployment-mcp-gateway.yml

# API
apply imagestream-api.yml
import_image ymir-api:prod quay.io/jotnar/ymir-api@sha256:206be70e91e1b07fc5f784bfed084a8355b11baac9947aeee3c910a018926888
apply configmap-api-oidc-env.yml
apply service-api.yml
apply route-api.yml
apply deployment-api.yml

# BeeAI Agents
apply imagestream-beeai-agent.yml
import_image beeai-agent:c9s quay.io/jotnar/beeai@sha256:c0f7feea8c7d23fd9aeaef42f3d023b52a978ef0308740f3254238d2ea1275e5
import_image beeai-agent:c10s quay.io/jotnar/beeai@sha256:6b4cd4a3a5ab6e1a50644fcb4ada35c6970a981ce6f0242da36f4751da3268d6
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
import_image jira-issue-fetcher:prod quay.io/jotnar/jira-issue-fetcher@sha256:d3efe22596397bd98d2b6b6f9ac764b36f3e6d49d2e965d3d37330dd3d46ce2f
apply configmap-jira-issue-fetcher-env.yml
apply configmap-jira-issue-fetcher-filter-env.yml
apply configmap-jira-issue-fetcher-todo-env.yml
apply cronjob-jira-issue-fetcher.yml
apply cronjob-jira-issue-fetcher-todo.yml

# MR Cleanup
apply imagestream-mr-cleanup.yml
import_image mr-cleanup:prod quay.io/jotnar/mr-cleanup@sha256:a1bc74b9e9b7a33f5437d28c014d14d6b4430683d4a18b960f8038a1870f0904
apply cronjob-mr-cleanup.yml

# Postponed Issue Sweeps
apply imagestream-sweep.yml
import_image sweep:prod quay.io/jotnar/sweep@sha256:fd343d8daaa582cc0f2eaf2254ad5a87404527557d1873917ffac3c3c5b038c2
apply cronjob-sweep-dependency.yml
apply cronjob-sweep-y-stream.yml
apply cronjob-sweep-pr-pending.yml
apply cronjob-sweep-no-patch.yml

# # Supervisor
# apply imagestream-supervisor.yml
# import_image supervisor
# apply deployment-supervisor-processor.yml
# apply cronjob-supervisor-collector.yml
