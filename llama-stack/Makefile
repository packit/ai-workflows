# AI Workflows - Simplified Makefile
# Usage: make <target>

COMPOSE ?= podman-compose
ISSUE ?= RHEL-78418
VENV ?= ai-workflows

.PHONY: bootstrap mcp-server llama-server workflow-runner run-all clean clean-all

# Bootstrap the virtual environment and install dependencies
bootstrap:
	@echo "🚀 Bootstrapping virtual environment: $(VENV)"
	@echo "📁 Creating virtual environment at ~/venv/$(VENV)..."
	python -m venv ~/venv/$(VENV)
	@echo "📦 Installing Llama Stack..."
	. ~/venv/$(VENV)/bin/activate && pip install --upgrade pip
	. ~/venv/$(VENV)/bin/activate && pip install llama-stack
	@if [ -f requirements.txt ]; then \
		echo "📦 Installing additional requirements from requirements.txt..."; \
		. ~/venv/$(VENV)/bin/activate && pip install -r requirements.txt; \
	fi
	@echo "✅ Virtual environment $(VENV) is ready!"
	@echo "💡 You can now run: make mcp-server && make llama-server && make agent"

# Start the Atlassian MCP server using podman-compose
mcp-server:
	@echo "🚀 Starting Atlassian MCP server..."
	$(COMPOSE) up -d mcp-atlassian
	@echo "✅ MCP server started at http://localhost:9000"

# Start the Llama Stack server locally
llama-server:
	@echo "🚀 Starting Llama Stack server locally..."
	@echo "📋 Using configuration: llama_stack_config.yaml"
	@echo "🔧 Virtual environment: $(VENV)"
	@if [ ! -d ~/venv/$(VENV) ]; then \
		echo "⚠️  Virtual environment ~/venv/$(VENV) not found"; \
		echo "💡 Run 'make bootstrap' first to create the virtual environment"; \
		exit 1; \
	fi
	@echo "⚠️  Make sure MCP server is running first (make mcp-server)"
	@if [ -f $(PWD)/.env ]; then \
		echo "📁 Loading environment from .env file..."; \
		set -a; . $(PWD)/.env; set +a; . ~/venv/$(VENV)/bin/activate && llama stack run llama_stack_config.yaml --port 8321; \
	else \
		echo "📁 No .env file found, using system environment"; \
		. ~/venv/$(VENV)/bin/activate && llama stack run llama_stack_config.yaml --port 8321; \
	fi

# Run the agent locally
workflow-runner:
	@echo "🤖 Running Jira analysis agent for issue: $(ISSUE)"
	@echo "🔧 Virtual environment: $(VENV)"
	@if [ ! -d ~/venv/$(VENV) ]; then \
		echo "⚠️  Virtual environment ~/venv/$(VENV) not found"; \
		echo "💡 Run 'make bootstrap' first to create the virtual environment"; \
		exit 1; \
	fi
	@echo "⚠️  Make sure Llama Stack server is running first (make llama-server)"
	@if [ -f $(PWD)/.env ]; then \
		echo "📁 Loading environment from .env file..."; \
		set -a; . $(PWD)/.env; set +a; . ~/venv/$(VENV)/bin/activate && python workflow_runner.py $(ISSUE); \
	else \
		echo "📁 No .env file found, using system environment"; \
		. ~/venv/$(VENV)/bin/activate && python workflow_runner.py $(ISSUE); \
	fi

# Run all services in the correct sequence
run-all:
	@echo "🎯 Starting complete AI workflow..."
	@echo "📋 Issue: $(ISSUE)"
	@echo "🔧 Virtual environment: $(VENV)"
	@if [ ! -d ~/venv/$(VENV) ]; then \
		echo "⚠️  Virtual environment ~/venv/$(VENV) not found"; \
		echo "💡 Run 'make bootstrap' first to create the virtual environment"; \
		exit 1; \
	fi
	@echo ""
	@echo "Step 1: Starting MCP server..."
	$(MAKE) mcp-server
	@echo ""
	@echo "Step 2: Starting Llama Stack server (this will block until stopped)..."
	@echo "💡 Open a new terminal and run: make agent ISSUE=$(ISSUE)"
	@echo "💡 Or press Ctrl+C to stop the server and run agent manually"
	$(MAKE) llama-server

# Clean up all services
clean:
	@echo "🧹 Cleaning up all services and containers..."
	@echo "Stopping and removing compose services..."
	$(COMPOSE) down --remove-orphans
	@echo "Removing any leftover MCP containers..."
	@-podman stop $$(podman ps -aq --filter "ancestor=ghcr.io/sooperset/mcp-atlassian:latest") 2>/dev/null || true
	@-podman rm $$(podman ps -aq --filter "ancestor=ghcr.io/sooperset/mcp-atlassian:latest") 2>/dev/null || true
	@echo "Removing any leftover llama-stack containers..."
	@-podman stop $$(podman ps -aq --filter "name=llama-stack") 2>/dev/null || true
	@-podman rm $$(podman ps -aq --filter "name=llama-stack") 2>/dev/null || true
	@echo "Pruning volumes..."
	podman volume prune -f
	@echo "Pruning unused networks..."
	podman network prune -f
	@echo "Stopping any local Llama Stack processes..."
	@-pkill -f "llama stack run" || true
	@echo "✅ Cleanup complete"

# Help target
help:
	@echo "Available targets:"
	@echo "  bootstrap                   - Bootstrap virtual environment and install dependencies"
	@echo "  mcp-server                  - Start MCP server in background"
	@echo "  llama-server                - Start Llama Stack server locally"
	@echo "  workflow-runner             - Run Jira analysis and rebase agents"
	@echo "  run-all                     - Run all services in correct sequence"
	@echo "  clean                       - Stop all services and clean up containers/volumes"
