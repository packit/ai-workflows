# AI Providers Data Flow

This document describes how the AI Workflows system interacts with AI model providers for automated decision-making and content generation.

## AI Provider Architecture

```mermaid
graph TD
    subgraph "Agent Framework"
        BEEAI["BeeAI Framework<br/>(Agent Orchestration)"]
        AGENTS["AI Agents<br/>(Rebase, Backport, MR, Triage)"]
    end

    subgraph "AI Model Access Layer"
        MCP["MCP Server<br/>(Gateway)"]
        CHAT["Chat Service<br/>(chat.py)"]
    end

    subgraph "Google Cloud Platform"
        VERTEX["Vertex AI<br/>(Gemini Models)"]
        GCP_AUTH["Service Account<br/>jotnar-vertex-prod.json"]
    end

    PHOENIX["Phoenix<br/>(Observability & Tracing)"]

    BEEAI -->|"Orchestrates"| AGENTS
    AGENTS -->|"Agent Requests"| MCP
    MCP -->|"Model Calls"| CHAT
    CHAT -->|"Authenticated API Calls"| GCP_AUTH
    GCP_AUTH -->|"API Request"| VERTEX
    VERTEX -->|"Model Response"| CHAT
    CHAT -->|"Response"| MCP
    MCP -->|"Tool Results"| AGENTS

    CHAT -.->|"Trace API Calls"| PHOENIX
    VERTEX -.->|"Log Responses"| PHOENIX

    style BEEAI fill:#f3e5f5,stroke:#7b1fa2,stroke-width:3px
    style AGENTS fill:#e8f5e9,stroke:#388e3c,stroke-width:3px
    style MCP fill:#fff4e6,stroke:#f57c00,stroke-width:3px
    style CHAT fill:#fff4e6,stroke:#f57c00,stroke-width:3px
    style VERTEX fill:#e3f2fd,stroke:#1976d2,stroke-width:3px
    style GCP_AUTH fill:#fff9c4,stroke:#f57f17,stroke-width:3px
    style PHOENIX fill:#fce4ec,stroke:#c2185b,stroke-width:3px
```

## Google Cloud Platform Integration

### Service Accounts

| Project | Purpose | API Key Storage |
|---------|---------|-----------------|
| **jotnar-bot** | Production deployment | Bitwarden: `jotnar-vertex-prod.json` |
| **packit-automated-packaging** | Development and testing | GCP Console |

### Authentication Flow

```mermaid
sequenceDiagram
    participant Agent as AI Agent
    participant Chat as Chat Service
    participant GCP as Google Cloud
    participant Vertex as Vertex AI API

    Agent->>Chat: Request model inference
    Chat->>Chat: Load service account key<br/>(jotnar-vertex-prod.json)
    Chat->>GCP: Authenticate with service account
    GCP-->>Chat: OAuth2 token
    Chat->>Vertex: API request with token
    Vertex->>Vertex: Execute model inference
    Vertex-->>Chat: Model response
    Chat-->>Agent: Processed response
```

## AI Model Usage

### Models in Use

**Primary Model:** Gemini (Google Vertex AI)

**Use Cases:**
- Spec file analysis and modification
- Patch backporting and application
- Build failure diagnosis and fixing
- Test result analysis
- Jira issue triage
- Merge request review

### Agent-to-Model Communication

```mermaid
flowchart LR
    TRIAGE["Triage Agent"]
    BACKPORT["Backport Agent"]
    REBASE["Rebase Agent"]
    MR_AGENT["MR Agent"]
    TESTING["Testing Analyst"]

    CHAT["Chat Service<br/>(Gemini)"]

    TRIAGE -->|"Analyze Jira issue<br/>Determine workflow"| CHAT
    BACKPORT -->|"Review patches<br/>Apply to spec"| CHAT
    REBASE -->|"Update version<br/>Resolve conflicts"| CHAT
    MR_AGENT -->|"Review MR comments<br/>Address feedback"| CHAT
    TESTING -->|"Analyze test results<br/>Triage failures"| CHAT

    CHAT -->|"Structured responses"| TRIAGE
    CHAT -->|"Patch recommendations"| BACKPORT
    CHAT -->|"Conflict resolution"| REBASE
    CHAT -->|"Action decisions"| MR_AGENT
    CHAT -->|"Test analysis"| TESTING

    style CHAT fill:#e3f2fd,stroke:#1976d2,stroke-width:3px
```

## Configuration

### Environment Variables

**Chat Service Configuration:**
```bash
# Model selection
CHAT_MODEL=gemini-1.5-pro

# API credentials
GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/jotnar-vertex-prod.json

# Model parameters
MAX_RETRIES=3
TEMPERATURE=0.7
```

**Agent Configuration:**
```bash
# BeeAI framework settings
BEEAI_MAX_ITERATIONS=10
BEEAI_TIMEOUT=300
```

### OpenShift Configuration

**Secrets:**
- `vertex-key` - Contains `jotnar-vertex-prod.json` service account key

**ConfigMaps:**
- `chat-env` - Chat model configuration
- `agents-env` - Agent-specific model parameters

---

**Last Updated:** 2026-03-03
