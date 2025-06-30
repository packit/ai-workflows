#!/bin/bash
set -e

# Simple Container Workflow Runner (No Redis)
# Uses Llama Stack's native session storage
# Usage: ./run_simple_workflow.sh <ISSUE_ID>

ISSUE_ID=${1:-RHEL-78418}

echo "🚀 Starting simple container workflow for issue: $ISSUE_ID"
echo "📋 Using Llama Stack's native session storage (no Redis needed)"

# Check if required environment variables are set
if [ -z "$GOOGLE_API_KEY" ]; then
    echo "❌ GOOGLE_API_KEY environment variable not set"
    exit 1
fi

if [ -z "$JIRA_PERSONAL_TOKEN" ]; then
    echo "❌ JIRA_PERSONAL_TOKEN environment variable not set"  
    exit 1
fi

# Use docker compose or docker-compose based on availability
DOCKER_COMPOSE_CMD="docker compose"
if ! command -v docker compose &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker-compose"
fi

echo "✅ Using Docker Compose command: $DOCKER_COMPOSE_CMD"

# Set environment variables
export ISSUE_ID=$ISSUE_ID
export GOOGLE_API_KEY=$GOOGLE_API_KEY
export JIRA_PERSONAL_TOKEN=$JIRA_PERSONAL_TOKEN
export INFERENCE_MODEL=${INFERENCE_MODEL:-gemini/gemini-2.5-pro}
export JIRA_URL=${JIRA_URL:-https://issues.redhat.com}

echo "📋 Configuration:"
echo "   Issue ID: $ISSUE_ID"
echo "   Model: $INFERENCE_MODEL"
echo "   Architecture: Simple (Llama Stack Sessions)"

# Cleanup function
cleanup() {
    echo "🧹 Cleaning up containers..."
    $DOCKER_COMPOSE_CMD -f docker-compose.simple.yaml down --volumes
}

# Set trap for cleanup on exit
trap cleanup EXIT

# Build and run
echo "🏗️ Building simple agent containers..."
$DOCKER_COMPOSE_CMD -f docker-compose.simple.yaml build

echo "🚀 Starting simple workflow..."
$DOCKER_COMPOSE_CMD -f docker-compose.simple.yaml up --abort-on-container-exit

# Check results
REBASE_EXIT_CODE=$($DOCKER_COMPOSE_CMD -f docker-compose.simple.yaml ps -q rebase-package | xargs docker inspect --format='{{.State.ExitCode}}' 2>/dev/null || echo "1")

if [ "$REBASE_EXIT_CODE" = "0" ]; then
    echo "🎉 Simple workflow completed successfully!"
    
    # Show workflow completion info
    if $DOCKER_COMPOSE_CMD -f docker-compose.simple.yaml exec -T rebase-package test -f /shared/workflow_complete.json 2>/dev/null; then
        echo "📋 Workflow Results:"
        $DOCKER_COMPOSE_CMD -f docker-compose.simple.yaml exec -T rebase-package cat /shared/workflow_complete.json 2>/dev/null || true
    fi
else
    echo "❌ Simple workflow failed"
    echo "📋 Container logs:"
    $DOCKER_COMPOSE_CMD -f docker-compose.simple.yaml logs
    exit 1
fi

echo "✅ Simple container workflow completed!"
echo ""
echo "🎯 Key Benefits:"
echo "  • No Redis dependency"
echo "  • Uses Llama Stack's native session storage"
echo "  • Simpler architecture"
echo "  • Automatic agent chaining via sessions"
echo ""
echo "📋 Session Management:"
echo "  • Sessions are stored in Llama Stack server"
echo "  • Agents can retrieve outputs from previous sessions"
echo "  • Persistent across container restarts" 