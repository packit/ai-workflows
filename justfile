#!/usr/bin/env just --justfile

#------------------------------------------------------------------------------
# Justfile for BeeAI Agent Development
#
# This file replaces Makefile and Makefile.tests, providing a single,
# consistent interface for building, running, and testing the BeeAI agents.
#
# USAGE:
# 1. Create a .env file to store your local configuration. You can start by
#    copying .env.example if it exists.
# 2. Run recipes using `just <recipe_name> [arguments...]`.
# 3. For a list of all available recipes, run `just` or `just --list`.
#------------------------------------------------------------------------------

set unstable
set lists

# --- Default Variables and Environment ---

# Use .env file for environment variables
# See https://just.systems/man/en/chapter_26.html
set export

# Shell variables, will be evaluated for every command that needs them
compose := shell('if podman compose ls >/dev/null 2>&1; then echo "podman compose"; elif command -v podman-compose >/dev/null 2>&1; then echo "podman-compose"; else echo "docker-compose"; fi')
container_tool := shell('command -v podman >/dev/null 2>&1 && echo "podman" || echo "docker"')

# Variables with defaults. These can be overridden in .env or on the command line
IMAGE_NAME := "beeai-agent"
compose_file := "compose.yaml"
DRY_RUN := "false"
MOCK_JIRA := "false"
JIRA_DRY_RUN := "false"
JIRA_ALLOW_STATUS_CHANGES := "false"
ERRATA_ALLOW_STATUS_CHANGES := "false"
AUTO_CHAIN := "true"
FORCE_CVE_TRIAGE := "false"
RUN_LLM_JUDGE := "true"
REGISTRY := "quay.io/jotnar"
TEST_IMAGE := "beeai-tests"
TEST_IMAGE_C9S := "beeai-tests-c9s"

# Internal compose commands
compose_agents := compose + ' -f ' + compose_file + ' --profile=agents'
compose_supervisor := compose + ' -f ' + compose_file + ' --profile=supervisor'

# --- Build and Push Images ---

# Build all service images (agents and supervisor)
build:
    @if [ -f .secrets/build.env ]; then echo "Warning: .secrets/build.env is deprecated, please move its contents to .env"; fi
    @if [ -f .secrets/build.env ] && [ ! -f .env ]; then set -a && . ./.secrets/build.env && set +a; fi && \
    {{compose}} -f {{compose_file}} --profile=agents --profile=supervisor build

# Push common images to the registry
push:
    @echo "Pushing images to {{REGISTRY}}..."
    {{container_tool}} push {{REGISTRY}}/phoenix:latest
    {{container_tool}} push {{REGISTRY}}/redis-commander:latest
    @echo "All images pushed successfully!"

# Build and push all images
build-and-push: build push

# --- Agent Standalone Runners ---

# Run the Triage Agent
run-triage-agent jira_issue:
    {{compose_agents}} run --rm \
        -e JIRA_ISSUE="{{jira_issue}}" \
        -e DRY_RUN={{DRY_RUN}} \
        -e MOCK_JIRA={{MOCK_JIRA}} \
        -e JIRA_DRY_RUN={{JIRA_DRY_RUN}} \
        -e FORCE_CVE_TRIAGE={{FORCE_CVE_TRIAGE}} \
        triage-agent

# Run the Rebase Agent for a given stream (c9s or c10s)
run-rebase-agent stream package version jira_issue branch justification:
    {{compose_agents}} run --rm \
        -e PACKAGE="{{package}}" \
        -e VERSION="{{version}}" \
        -e JIRA_ISSUE="{{jira_issue}}" \
        -e BRANCH="{{branch}}" \
        -e DRY_RUN={{DRY_RUN}} \
        -e MOCK_JIRA={{MOCK_JIRA}} \
        -e JIRA_DRY_RUN={{JIRA_DRY_RUN}} \
        -e "JUSTIFICATION={{justification}}" \
        rebase-agent-{{stream}}

# Run the Backport Agent for a given stream (c9s or c10s)
run-backport-agent stream package upstream_patches jira_issue branch justification cve_id='':
    {{compose_agents}}  run --rm \
        -e PACKAGE="{{package}}" \
        -e UPSTREAM_PATCHES="{{upstream_patches}}" \
        -e JIRA_ISSUE="{{jira_issue}}" \
        -e BRANCH="{{branch}}" \
        -e DRY_RUN={{DRY_RUN}} \
        -e MOCK_JIRA={{MOCK_JIRA}} \
        -e JIRA_DRY_RUN={{JIRA_DRY_RUN}} \
        -e CVE_ID="{{cve_id}}" \
        -e "JUSTIFICATION={{justification}}" \
        backport-agent-{{stream}}

# Run the Rebuild Agent for a given stream (c9s or c10s)
run-rebuild-agent stream package jira_issue branch justification dependency_issue='' dependency_component='' consolidated_issues='':
    {{compose_agents}} run --rm \
        -e PACKAGE="{{package}}" \
        -e JIRA_ISSUE="{{jira_issue}}" \
        -e BRANCH="{{branch}}" \
        -e DRY_RUN={{DRY_RUN}} \
        -e MOCK_JIRA={{MOCK_JIRA}} \
        -e DEPENDENCY_ISSUE="{{dependency_issue}}" \
        -e DEPENDENCY_COMPONENT="{{dependency_component}}" \
        -e CONSOLIDATED_ISSUES="{{consolidated_issues}}" \
        -e JIRA_DRY_RUN={{JIRA_DRY_RUN}} \
        -e "JUSTIFICATION={{justification}}" \
        rebuild-agent-{{stream}}

# Run the MR Agent for a given stream (c9s or c10s)
run-mr-agent stream merge_request_url:
    {{compose_agents}} run --rm \
        -e MERGE_REQUEST_URL="{{merge_request_url}}" \
        mr-agent-{{stream}}

# --- E2E Tests ---

# Run Triage Agent E2E tests
run-triage-agent-e2e-tests:
    @echo "SAFETY: MOCK_JIRA=true and DRY_RUN=true are enforced for E2E tests."
    MOCK_JIRA=true DRY_RUN=true {{compose}} -f {{compose_file}} --profile=e2e-test run --rm \
        -e MOCK_JIRA="true" \
        -e DRY_RUN="true" \
        triage-agent-e2e-tests

# Run Backport Agent E2E tests
run-backport-agent-e2e-tests:
    @echo "SAFETY: MOCK_JIRA=true and DRY_RUN=true are enforced for E2E tests."
    MOCK_JIRA=true DRY_RUN=true {{compose}} -f {{compose_file}} --profile=e2e-test run --rm \
        -e MOCK_JIRA="true" \
        -e DRY_RUN="true" \
        -e RUN_LLM_JUDGE={{RUN_LLM_JUDGE}} \
        backport-agent-e2e-tests

# --- Development Lifecycle ---

# Start all agent services in the foreground
start:
    DRY_RUN={{DRY_RUN}} MOCK_JIRA={{MOCK_JIRA}} JIRA_DRY_RUN={{JIRA_DRY_RUN}} JIRA_ALLOW_STATUS_CHANGES={{JIRA_ALLOW_STATUS_CHANGES}} ERRATA_ALLOW_STATUS_CHANGES={{ERRATA_ALLOW_STATUS_CHANGES}} AUTO_CHAIN={{AUTO_CHAIN}} {{compose_agents}} up

# Start all agent services in detached mode
start-detached:
    DRY_RUN={{DRY_RUN}} MOCK_JIRA={{MOCK_JIRA}} JIRA_DRY_RUN={{JIRA_DRY_RUN}} JIRA_ALLOW_STATUS_CHANGES={{JIRA_ALLOW_STATUS_CHANGES}} ERRATA_ALLOW_STATUS_CHANGES={{ERRATA_ALLOW_STATUS_CHANGES}} AUTO_CHAIN={{AUTO_CHAIN}} {{compose_agents}} up -d

# Stop and remove all services
stop:
    {{compose_agents}} stop
    {{compose_agents}} down

# Stop and remove all services, including volumes
clean:
    {{compose}} -f {{compose_file}} down --volumes

# View the status of running services
status:
    {{compose}} -f {{compose_file}} ps

# --- Logging ---

# Follow logs for a specific agent
logs agent:
    {{compose_agents}} logs -f {{agent}}

# --- Pipeline and Supervisor ---

# Trigger a pipeline for a Jira issue
trigger-pipeline jira_issue:
    @echo "Triggering pipeline for issue: {{jira_issue}} (force_cve_triage={{FORCE_CVE_TRIAGE}})"
    {{compose_agents}} exec valkey redis-cli LPUSH triage_queue '{"metadata": {"issue": "{{jira_issue}}", "force_cve_triage": {{FORCE_CVE_TRIAGE}}}}'

# Clear the supervisor queue
supervisor-clear-queue debug='false':
    {{compose_supervisor}} run --rm \
        supervisor python -m ymir.supervisor.main {{ (if debug == 'true' { '--debug' }) }} clear-queue

# Collect issues/errata for processing
supervisor-collect debug='false':
    {{compose_supervisor}} run --rm \
        supervisor python -m ymir.supervisor.main {{ (if debug == 'true' { '--debug' }) }} collect --no-repeat

# Process a single Jira issue
process-issue jira_issue ignore_needs_attention='false' dry_run_flag='true' debug='false':
    {{compose_supervisor}} run --rm \
        supervisor python -m ymir.supervisor.main {{ (if debug == 'true' { '--debug' }) }} {{ (if ignore_needs_attention == 'true' { '--ignore-needs-attention' }) }} {{ (if dry_run_flag == 'true' { '--dry-run' }) }} process-issue {{jira_issue}}

# Process a single Erratum
process-erratum errata_id ignore_needs_attention='false' dry_run_flag='true' debug='false':
    {{compose_supervisor}} run --rm \
        supervisor python -m ymir.supervisor.main {{ (if debug == 'true' { '--debug' }) }} {{ (if ignore_needs_attention == 'true' { '--ignore-needs-attention' }) }} {{ (if dry_run_flag == 'true' { '--dry-run' }) }} process-erratum {{errata_id}}

# --- Testing (from Makefile.tests) ---

# Build the test images
build-test-images: build-test-image-fedora build-test-image-c9s

# Build the Fedora test image
build-test-image-fedora:
    {{container_tool}} build --rm --tag {{TEST_IMAGE}} -f Containerfile.tests

# Build the CentOS Stream 9 test image
build-test-image-c9s:
    {{container_tool}} build --rm --tag {{TEST_IMAGE_C9S}} -f Containerfile.c9s-tests

# Run all local tests
check: check-agents check-unprivileged-tools check-privileged-tools check-jira-issue-fetcher check-ymir-common check-supervisor check-mcp-install

# Run tests for a specific component locally
check-agents: (_run-tests 'ymir/agents/')
check-unprivileged-tools: (_run-tests 'ymir/tools/unprivileged/')
check-privileged-tools: (_run-tests 'ymir/tools/privileged/')
check-jira-issue-fetcher: (_run-tests 'ymir/jira_issue_fetcher/')
check-ymir-common: (_run-tests 'ymir/common/')
check-supervisor: (_run-tests 'ymir/supervisor/')
check-mcp-install:
    @PYTHONPATH= bash scripts/test_mcp_install.sh

# [Internal] Helper to run pytest
_run-tests test_path:
    PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 python3 -m pytest --verbose --showlocals {{test_path}}tests/unit

# Run all tests in their respective containers
check-in-container: build-test-images
    just check-agents-in-container
    just check-unprivileged-tools-in-container
    just check-privileged-tools-in-container
    just check-jira-issue-fetcher-in-container
    just check-ymir-common-in-container
    just check-supervisor-in-container
    just check-mcp-install-in-container

# Run component tests in containers
check-agents-in-container: (_run-tests-in-container TEST_IMAGE_C9S 'check-agents')
check-unprivileged-tools-in-container: (_run-tests-in-container TEST_IMAGE_C9S 'check-unprivileged-tools')
check-privileged-tools-in-container: (_run-tests-in-container TEST_IMAGE 'check-privileged-tools')
check-jira-issue-fetcher-in-container: (_run-tests-in-container TEST_IMAGE 'check-jira-issue-fetcher')
check-ymir-common-in-container: (_run-tests-in-container TEST_IMAGE_C9S 'check-ymir-common')
check-supervisor-in-container: (_run-tests-in-container TEST_IMAGE_C9S 'check-supervisor')
check-mcp-install-in-container: (_run-tests-in-container TEST_IMAGE 'check-mcp-install')

# [Internal] Helper to run tests in a container
_run-tests-in-container image recipe:
    {{container_tool}} run --rm -it -v $(pwd):/src:z -w /src --env TEST_TARGET {{image}} just {{recipe}}

# --- Utilities ---

# Open a bash shell in the triage-agent container
bash:
    {{compose_agents}} run --rm triage-agent /bin/bash

# Open a redis-cli session
redis-cli:
    {{compose_agents}} exec valkey redis-cli
