# ADK CentOS Package Updater Agent

This Google ADK agent automates the process of updating CentOS packages to newer versions and preparing them for merge requests.

## Quick Start (Container)

1. **Build the container:**
   ```bash
   cd adk-workflows
   make build
   ```

2. **Configure environment:**
   ```bash
   cp env.template .env
   # Edit .env with your values (GOOGLE_API_KEY, PACKAGE, VERSION, etc.)
   ```

3. **Run the agent:**
   ```bash
   make adk-package-updater
   ```

After this I needed to input prompt `run agent`, need to check how to avoid this.
