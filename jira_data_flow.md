# Jira Data Flow Chart

This document describes the data flow between the AI Workflows system and Jira service.

## System Architecture

```mermaid
graph TD
    JIRA[("Jira Service<br/>issues.redhat.com")]

    JIRA -->|"Direct API<br/>Search Issues"| FETCHER
    JIRA <-->|"Direct API<br/>Monitor & Update"| SUPERVISOR
    JIRA <-->|"API Calls via<br/>MCP Tools"| MCP

    FETCHER["Jira Issue Fetcher<br/>(Cron Job)<br/>jira_issue_fetcher.py<br/><br/>Direct API Access"]

    FETCHER -->|"Push Tasks"| REDIS_IN

    subgraph Redis["Redis Queues"]
        direction TB
        REDIS_IN["Input Queues:<br/>triage_queue"]
        REDIS_OUT["Output Queues:<br/>rebase_queue_*<br/>backport_queue_*<br/>clarification_needed_queue<br/>error_list<br/>no_action_list"]
    end

    REDIS_IN -->|"Consume"| AGENTS

    AGENTS["AI Agents:<br/>Triage Agent<br/>Rebase Agent<br/>Backport Agent<br/><br/>Use MCP Tools Only"]

    AGENTS -->|"Call Tools"| MCP
    AGENTS -->|"Push Results"| REDIS_OUT

    MCP["MCP Server<br/>(FastMCP Tools)<br/>jira_tools.py<br/><br/>For AI Agents Only"]

    SUPERVISOR["Supervisor<br/>(Workflow Orchestrator)<br/>jira_utils.py<br/><br/>Direct API Access"]

    SUPERVISOR -->|"Check Queues"| Redis

    style FETCHER fill:#e8f4f8,stroke:#0277bd,stroke-width:3px
    style SUPERVISOR fill:#e8f4f8,stroke:#0277bd,stroke-width:3px
    style MCP fill:#fff4e6,stroke:#f57c00,stroke-width:3px
    style AGENTS fill:#e8f5e9,stroke:#388e3c,stroke-width:3px
    style JIRA fill:#ffebee,stroke:#c62828,stroke-width:3px
```

**Key Distinctions:**
- **AI Agents** - Use MCP Server tools to access Jira indirectly
- **Python Services** - Direct HTTP API calls to Jira using requests library
- **MCP Server** - Provides controlled FastMCP tools for AI agents only

## Component Types

### AI Agents (Use MCP Server)

These are AI-powered agents that use the MCP Server tools to interact with Jira:
- **Triage Agent** - Analyzes issues, determines if rebase/backport/no-action needed
- **Rebase Agent** - Updates packages to new upstream versions
- **Backport Agent** - Applies specific patches to packages

*These agents access Jira ONLY through MCP tools like `get_jira_details()`, `add_jira_comment()`, etc.*

### Python Services (Direct Jira API Access)

Traditional Python services that make direct HTTP calls to Jira:
- **Jira Issue Fetcher** - Periodic/cron job that queries Jira for assigned issues and pushes them to Redis triage queue. Uses `requests` library for direct API calls.
- **Supervisor** - Workflow orchestration service that monitors issues/errata, advances them through testing/release process. Makes direct API calls using `requests` library via functions like `jira_api_get()`, `jira_api_post()`, `jira_api_put()`.

*These services do NOT use the MCP Server - they call Jira API directly.*

### MCP Server

FastMCP server that provides controlled tools for AI agents:
- Exposes 7 Jira-related tools (see MCP Server Tool Summary below)
- Uses `aiohttp` for async HTTP calls to Jira
- Only used by AI agents, not by Python services

## Data Flow Overview

### 1. Jira Issue Fetcher → Jira (READ)

```mermaid
sequenceDiagram
    participant Fetcher as Jira Issue Fetcher
    participant Jira as Jira API
    participant Redis as Redis Queue

    Fetcher->>Jira: POST /rest/api/2/search<br/>JQL: "project=RHEL and assignee=jotnar-project"
    Jira-->>Fetcher: Issues [{key, labels}]
    Fetcher->>Fetcher: Deduplicate & Filter<br/>(skip if jötnar labels exist)
    Fetcher->>Redis: Push Task to triage_queue<br/>{metadata: {issue: "RHEL-12345"}}
```

**Key Features:**
- Pagination: 500 issues/page
- Rate limiting: 5 calls/second
- Exponential backoff on failures
- Deduplication across all Redis queues

### 2. MCP Server Tools → Jira (READ/WRITE)

```mermaid
sequenceDiagram
    participant Agent as AI Agent
    participant MCP as MCP Server
    participant Jira as Jira API

    rect rgb(200, 220, 240)
        Note over Agent,Jira: READ Operations
        Agent->>MCP: get_jira_details(issue_key)
        MCP->>Jira: GET /rest/api/2/issue/{key}?expand=comments
        MCP->>Jira: GET /rest/api/2/issue/{key}/remotelink
        Jira-->>MCP: Issue data + comments + links
        MCP-->>Agent: Complete issue details

        Agent->>MCP: check_cve_triage_eligibility(issue_key)
        MCP->>Jira: GET /rest/api/2/issue/{key}
        Jira-->>MCP: Issue fields (labels, fixVersions, severity)
        MCP-->>Agent: CVEEligibilityResult
    end

    rect rgb(240, 220, 200)
        Note over Agent,Jira: WRITE Operations
        Agent->>MCP: add_jira_comment(issue_key, comment)
        MCP->>Jira: POST /rest/api/2/issue/{key}/comment
        Jira-->>MCP: Success

        Agent->>MCP: edit_jira_labels(issue_key, add, remove)
        MCP->>Jira: PUT /rest/api/2/issue/{key}<br/>{update: {labels: [...]}}
        Jira-->>MCP: Success

        Agent->>MCP: change_jira_status(issue_key, status)
        MCP->>Jira: GET /rest/api/2/issue/{key}/transitions
        Jira-->>MCP: Available transitions
        MCP->>Jira: POST /rest/api/2/issue/{key}/transitions
        Jira-->>MCP: Success
    end
```

### 3. Supervisor → Jira (READ/WRITE)

```mermaid
sequenceDiagram
    participant Supervisor
    participant Jira as Jira API

    Supervisor->>Jira: POST /rest/api/2/search<br/>(get_current_issues with JQL)
    Jira-->>Supervisor: Issues with full fields

    Supervisor->>Supervisor: Process workflow logic

    Supervisor->>Jira: PUT /rest/api/2/issue/{key}<br/>(add/remove labels)
    Supervisor->>Jira: POST /rest/api/2/issue/{key}/comment<br/>(add progress updates)
    Supervisor->>Jira: POST /rest/api/2/issue/{key}/transitions<br/>(change status)
    Supervisor->>Jira: POST /rest/api/2/issue/{key}/attachments<br/>(upload files)
```

## Complete Workflow

```mermaid
flowchart TD
    START([Jira Issue Created])
    FETCH[Jira Issue Fetcher<br/>Queries Jira]
    FILTER{Has jötnar<br/>labels?}
    REDIS[(Redis<br/>triage_queue)]
    TRIAGE[Triage Agent]
    DECIDE{Resolution<br/>Type?}

    REBASE[Rebase Queue]
    BACKPORT[Backport Queue]
    CLARIFY[Clarification Queue]
    NOACTION[No Action List]
    ERROR[Error List]

    PROCESS[Rebase/Backport<br/>Agents]
    SUPER[Supervisor]
    UPDATE[Update Jira]

    START --> FETCH
    FETCH --> FILTER
    FILTER -->|No or retry_needed| REDIS
    FILTER -->|Yes| START
    REDIS --> TRIAGE
    TRIAGE --> DECIDE

    DECIDE -->|Rebase| REBASE
    DECIDE -->|Backport| BACKPORT
    DECIDE -->|Needs Info| CLARIFY
    DECIDE -->|No Action| NOACTION
    DECIDE -->|Error| ERROR

    REBASE --> PROCESS
    BACKPORT --> PROCESS
    PROCESS --> UPDATE

    SUPER -.Monitor.-> REBASE
    SUPER -.Monitor.-> BACKPORT
    SUPER --> UPDATE

    UPDATE --> |Labels, Comments,<br/>Status, Fields| START
```

## MCP Server Tool Summary

| Tool | Method | Endpoint | Purpose |
|------|--------|----------|---------|
| **get_jira_details** | GET | `/rest/api/2/issue/{key}` | Fetch issue details, comments, remote links |
| **check_cve_triage_eligibility** | GET | `/rest/api/2/issue/{key}` | Analyze CVE eligibility for triage |
| **verify_issue_author** | GET | `/rest/api/2/user` | Check if author is Red Hat employee |
| **set_jira_fields** | PUT | `/rest/api/2/issue/{key}` | Update fixVersions, severity, target_end |
| **add_jira_comment** | POST | `/rest/api/2/issue/{key}/comment` | Add public/private comment |
| **change_jira_status** | POST | `/rest/api/2/issue/{key}/transitions` | Transition issue status |
| **edit_jira_labels** | PUT | `/rest/api/2/issue/{key}` | Add/remove labels |

## Authentication & Configuration

All components authenticate using:
- **Bearer Token**: `JIRA_TOKEN` environment variable
- **Base URL**: `JIRA_URL` (default: https://issues.redhat.com)
- **Headers**:
  ```json
  {
    "Authorization": "Bearer {JIRA_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json"
  }
  ```

---

**Last Updated:** 2026-03-03
