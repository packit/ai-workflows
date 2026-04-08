# GitLab Dist-Git Data Flow

This document describes how the AI Workflows system interacts with GitLab for CentOS Stream and RHEL dist-git repositories.

## Repository Structure

```mermaid
graph TD
    GITLAB["GitLab<br/>(gitlab.com)"]

    subgraph "Upstream Repositories"
        CENTOS["CentOS Stream Dist-Git<br/>gitlab.com/redhat/centos-stream/rpms/*"]
        RHEL["RHEL Dist-Git<br/>gitlab.com/redhat/rhel/rpms/*<br/>(Internal)"]
    end

    subgraph "Bot Forks"
        FORK_CENTOS["Fork: centos_rpms_*<br/>gitlab.com/jotnar-bot/"]
        FORK_RHEL["Fork: rhel_rpms_*<br/>gitlab.com/jotnar-bot/"]
    end

    GITLAB --> CENTOS
    GITLAB --> RHEL
    CENTOS -.Fork.-> FORK_CENTOS
    RHEL -.Fork.-> FORK_RHEL

    FORK_CENTOS -.Merge Request.-> CENTOS
    FORK_RHEL -.Merge Request.-> RHEL

    style CENTOS fill:#e1f5fe
    style RHEL fill:#f3e5f5
    style FORK_CENTOS fill:#c8e6c9
    style FORK_RHEL fill:#c8e6c9
```

## System Architecture

```mermaid
graph TD
    GITLAB[("GitLab<br/>gitlab.com/redhat")]

    subgraph "Python Services (Direct Git/API)"
        SUPERVISOR["Supervisor<br/>(gitlab_utils.py)"]
    end

    subgraph "MCP Server Layer"
        MCP_GITLAB["GitLab Tools<br/>(gitlab_tools.py)"]
        MCP_DISTGIT["Dist-Git Tools<br/>(distgit_tools.py)"]
    end

    subgraph "AI Agents (Use MCP Tools)"
        AGENTS["Rebase Agent<br/>Backport Agent<br/>MR Agent"]
    end

    SUPERVISOR <-->|"Direct API<br/>Search MRs"| GITLAB

    AGENTS -->|"Use MCP Tools"| MCP_GITLAB
    AGENTS -->|"Use MCP Tools"| MCP_DISTGIT

    MCP_GITLAB <-->|"Git & API Operations<br/>Fork, Clone, Push,<br/>Open MR, Comments"| GITLAB
    MCP_DISTGIT <-->|"Create Z-Stream<br/>Branches"| GITLAB

    style SUPERVISOR fill:#e8f4f8,stroke:#0277bd,stroke-width:3px
    style MCP_GITLAB fill:#fff4e6,stroke:#f57c00,stroke-width:3px
    style MCP_DISTGIT fill:#fff4e6,stroke:#f57c00,stroke-width:3px
    style AGENTS fill:#e8f5e9,stroke:#388e3c,stroke-width:3px
    style GITLAB fill:#ffebee,stroke:#c62828,stroke-width:3px
```

## MCP Server GitLab Tools

### Git Operations

| Tool | Purpose | API/Git |
|------|---------|---------|
| **fork_repository** | Create or get existing fork | GitLab API |
| **clone_repository** | Clone repo to local path | Git CLI |
| **push_to_remote_repository** | Push branch to remote | Git CLI |

### Merge Request Management

| Tool | Purpose | Returns |
|------|---------|---------|
| **open_merge_request** | Create MR from fork to upstream | MR URL, is_new |
| **get_merge_request_details** | Get MR source/target/comments | MergeRequestDetails |
| **add_merge_request_comment** | Add comment to MR | Success message |
| **add_blocking_merge_request_comment** | Add unresolved discussion (blocks merge) | Success message |
| **add_merge_request_labels** | Add labels to MR | Success message |
| **create_merge_request_checklist** | Add internal checklist note | Success message |

### Pipeline Operations

| Tool | Purpose | Returns |
|------|---------|---------|
| **get_failed_pipeline_jobs_from_merge_request** | Get failed CI jobs | List[FailedPipelineJob] |
| **retry_pipeline_job** | Retry specific pipeline job | Job status |

### Branch Operations

| Tool | Purpose | Returns |
|------|---------|---------|
| **get_internal_rhel_branches** | List RHEL dist-git branches | List[str] |
| **create_zstream_branch** (distgit_tools) | Create new Z-Stream branch | Success message |

## Workflow: Rebase/Backport to Merge Request

```mermaid
sequenceDiagram
    participant Agent as Rebase/Backport Agent
    participant MCP_Git as GitLab Tools (MCP)
    participant GitLab as GitLab API/Git
    participant Fork as Bot Fork
    participant Upstream as Upstream Repo

    Agent->>MCP_Git: fork_repository(upstream_url)
    MCP_Git->>GitLab: Check if fork exists
    alt Fork exists
        GitLab-->>MCP_Git: Return existing fork URL
    else Fork doesn't exist
        MCP_Git->>GitLab: Create new fork
        GitLab-->>MCP_Git: Return new fork URL
    end
    MCP_Git-->>Agent: Fork URL

    Agent->>MCP_Git: clone_repository(fork_url, branch, path)
    MCP_Git->>Fork: git init && git fetch
    Fork-->>MCP_Git: Clone successful
    MCP_Git-->>Agent: Cloned to path

    Note over Agent: Agent modifies files<br/>(rebase or backport)

    Agent->>MCP_Git: push_to_remote_repository(fork_url, path, branch)
    MCP_Git->>Fork: git push
    Fork-->>MCP_Git: Push successful
    MCP_Git-->>Agent: Pushed successfully

    Agent->>MCP_Git: open_merge_request(fork_url, title, desc, target, source)
    MCP_Git->>Upstream: Create MR or update existing
    MCP_Git->>Upstream: Add label: jotnar_needs_attention
    Upstream-->>MCP_Git: MR URL
    MCP_Git-->>Agent: MR URL, is_brand_new

    Agent->>MCP_Git: create_merge_request_checklist(mr_url, checklist)
    MCP_Git->>Upstream: Create internal note with checklist
    Upstream-->>MCP_Git: Success
    MCP_Git-->>Agent: Checklist created
```

## Workflow: Merge Request Review Updates

```mermaid
sequenceDiagram
    participant MR_Agent as MR Agent
    participant MCP as GitLab Tools (MCP)
    participant GitLab as GitLab MR

    MR_Agent->>MCP: get_merge_request_details(mr_url)
    MCP->>GitLab: GET merge request details
    MCP->>GitLab: GET authorized comments (Developer+)
    GitLab-->>MCP: MR details + comments mentioning bot
    MCP-->>MR_Agent: MergeRequestDetails

    Note over MR_Agent: Analyze comments<br/>Determine actions needed

    alt Failed Pipeline
        MR_Agent->>MCP: get_failed_pipeline_jobs_from_merge_request(mr_url)
        MCP->>GitLab: GET latest pipeline jobs
        GitLab-->>MCP: List of failed jobs
        MCP-->>MR_Agent: List[FailedPipelineJob]

        MR_Agent->>MCP: add_blocking_merge_request_comment(mr_url, analysis)
        MCP->>GitLab: POST unresolved discussion
        GitLab-->>MCP: Discussion created
    end

    alt Reviewer Requested Changes
        MR_Agent->>MCP: clone_repository(source_repo, branch, path)
        Note over MR_Agent: Make requested changes
        MR_Agent->>MCP: push_to_remote_repository(fork_url, path, branch)
        MR_Agent->>MCP: add_merge_request_comment(mr_url, "Changes applied")
    end
```

## Supervisor GitLab Operations

The Supervisor uses direct GitLab API calls (not MCP) for:

```mermaid
flowchart LR
    SUPERVISOR[Supervisor]

    SUPERVISOR -->|"search_gitlab_project_mrs()"| SEARCH["Search MRs by<br/>issue key"]

    SEARCH --> CHECK{MR State?}

    CHECK -->|Opened| MONITOR["Monitor MR<br/>for merge"]
    CHECK -->|Merged| TRACK["Track merged<br/>build"]

    style SUPERVISOR fill:#e8f4f8
    style SEARCH fill:#fff9c4
    style MONITOR fill:#e1f5fe
    style TRACK fill:#c8e6c9
```

**Function:** `search_gitlab_project_mrs(project, issue_key, state)`

- **Purpose:** Find merge requests related to a Jira issue
- **API Call:** `GET /api/v4/projects/{project}/merge_requests?search={issue_key}`
- **Returns:** Iterator of MergeRequest objects
- **Use Case:** Supervisor tracks MR state to advance issue workflow

## Repository Naming Conventions

### Upstream Repositories

| Type | Pattern | Example |
|------|---------|---------|
| CentOS Stream | `gitlab.com/redhat/centos-stream/rpms/{package}` | `gitlab.com/redhat/centos-stream/rpms/bash` |
| RHEL | `gitlab.com/redhat/rhel/rpms/{package}` | `gitlab.com/redhat/rhel/rpms/bash` |

### Bot Forks

Fork naming follows `centpkg fork` convention:

| Upstream | Fork Name | Example |
|----------|-----------|---------|
| `redhat/centos-stream/rpms/bash` | `centos_rpms_bash` | `gitlab.com/jotnar-bot/centos_rpms_bash` |
| `redhat/rhel/rpms/bash` | `rhel_rpms_bash` | `gitlab.com/jotnar-bot/rhel_rpms_bash` |

**Pattern:** The fork name is constructed from the upstream path. The path segments after `redhat/` are joined with an underscore (`_`) to form a prefix, with `centos-stream` being shortened to `centos`. This prefix is then combined with the package name, following the format `{namespace_prefix}_{package}`.

## Branch Naming

### CentOS Stream Branches

- `c9s` - CentOS Stream 9
- `c10s` - CentOS Stream 10

### RHEL Branches

- `rhel-9-main` - RHEL 9 Y-stream (latest minor version)
- `rhel-9.8.0` - RHEL 9.8 Z-stream
- `rhel-10-main` - RHEL 10 Y-stream

## Authentication

### GitLab API Token

- **Environment Variable:** `GITLAB_TOKEN`
- **Used By:** MCP Server, Supervisor
- **Format:** OAuth2 token
- **Scope:** API access, fork creation, MR management

### Git Operations

**HTTPS (MCP Server):**
```
https://oauth2:{GITLAB_TOKEN}@gitlab.com/...
```

**SSH (Dist-Git Tools):**
```
ssh://{username}@pkgs.devel.redhat.com/rpms/{package}
```
- Requires Kerberos authentication
- Used for internal RHEL dist-git operations

## Merge Request Labels

| Label | Applied By | Meaning |
|-------|------------|---------|
| `jotnar_needs_attention` | open_merge_request (auto) | MR needs human review |
| `jotnar_needs_inspection` | Agents | Specific attention required |
| `target::latest` | GitLab CI (auto) | Y-stream build |
| `target::zstream` | GitLab CI (auto) | Z-stream build |
| `feature::draft-builds::enabled` | Manual (when ready) | Enables Konflux draft builds when added |

---

**Last Updated:** 2026-03-03
