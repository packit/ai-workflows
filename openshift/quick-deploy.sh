#!/bin/sh

set -e

oc project jotnar-prod

oc apply -n jotnar-prod -f deployment-backport-agent-c10s.yml
oc apply -n jotnar-prod -f deployment-backport-agent-c9s.yml
oc apply -n jotnar-prod -f deployment-rebase-agent-c10s.yml
oc apply -n jotnar-prod -f deployment-rebase-agent-c9s.yml
oc apply -n jotnar-prod -f deployment-mcp-gateway.yml
oc apply -n jotnar-prod -f deployment-triage-agent.yml
