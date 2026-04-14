#!/bin/sh

set -e

oc project jotnar-ymir--jotnar-ymir

oc import-image beeai-agent --all
oc import-image mcp-server --all
oc apply -n jotnar-ymir--jotnar-ymir -f deployment-backport-agent-c10s.yml
oc apply -n jotnar-ymir--jotnar-ymir -f deployment-backport-agent-c9s.yml
oc apply -n jotnar-ymir--jotnar-ymir -f deployment-rebase-agent-c10s.yml
oc apply -n jotnar-ymir--jotnar-ymir -f deployment-rebase-agent-c9s.yml
oc apply -n jotnar-ymir--jotnar-ymir -f deployment-mcp-gateway.yml
oc apply -n jotnar-ymir--jotnar-ymir -f deployment-triage-agent.yml
