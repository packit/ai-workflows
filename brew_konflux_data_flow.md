# Brew/Konflux Build System Data Flow

This document describes how the AI Workflows system interacts with Brew (Koji) and Konflux for RHEL package builds.

## System Architecture

```mermaid
graph TD
    BREW[("Brew/Koji<br/>brewhub.engineering.redhat.com")]
    KONFLUX[("Konflux<br/>Build System")]

    subgraph "GitLab CI Pipeline"
        PIPELINE["build_rpm job"]
    end

    subgraph "Python Services (Direct API)"
        SUPERVISOR["Supervisor<br/>Monitors builds"]
    end

    subgraph "MCP Server Layer"
        MCP_DISTGIT["Dist-Git Tools<br/>(distgit_tools.py)"]
        MCP_GITLAB["GitLab Tools<br/>(gitlab_tools.py)"]
    end

    subgraph "AI Agents"
        AGENTS["Rebase/Backport<br/>Agents"]
    end

    AGENTS -->|"Add MR label"| MCP_GITLAB
    MCP_GITLAB -->|"Add label:<br/>feature::draft-builds::enabled"| PIPELINE

    PIPELINE -->|"Trigger build"| KONFLUX
    KONFLUX -->|"Build complete"| PIPELINE

    PIPELINE -.Also triggers.-> BREW
    BREW -.RHEL builds.-> PIPELINE

    MCP_DISTGIT <-->|"Koji Client<br/>Build metadata<br/>Tag queries"| BREW

    SUPERVISOR -.Monitor MR.-> PIPELINE

    style SUPERVISOR fill:#e8f4f8,stroke:#0277bd,stroke-width:3px
    style MCP_DISTGIT fill:#fff4e6,stroke:#f57c00,stroke-width:3px
    style MCP_GITLAB fill:#fff4e6,stroke:#f57c00,stroke-width:3px
    style AGENTS fill:#e8f5e9,stroke:#388e3c,stroke-width:3px
    style BREW fill:#fff9c4,stroke:#f57c00,stroke-width:3px
    style KONFLUX fill:#e1f5fe,stroke:#0277bd,stroke-width:3px
    style PIPELINE fill:#f3e5f5,stroke:#8e24aa,stroke-width:3px
```

## Build Trigger Flow

```mermaid
sequenceDiagram
    participant Agent as Rebase/Backport Agent
    participant MCP as GitLab Tools (MCP)
    participant MR as GitLab MR
    participant CI as GitLab CI Pipeline
    participant Konflux
    participant Brew as Brew/Koji

    Agent->>MCP: open_merge_request()
    MCP->>MR: Create MR
    MCP->>MR: Add label: jotnar_needs_attention
    Note over MR: MR created without build label

    Note over MR: Later, when ready for build...

    MCP->>MR: Add label: feature::draft-builds::enabled
    Note over MR: Label triggers CI pipeline

    MR->>CI: Pipeline triggered
    CI->>CI: Run build_rpm job

    CI->>Konflux: Submit draft build
    Note over Konflux: Build package RPMs
    Konflux-->>CI: Build artifacts

    alt Draft build succeeds
        CI->>CI: Run gating tests
        Note over CI: Automated testing

        alt Gating passes
            CI->>Brew: Trigger RHEL build
            Note over Brew: Official RHEL build for erratum
            Brew-->>CI: Build NVR
        end
    end

    CI->>MR: Update pipeline status
```

## MR Labels

| Label | Applied By | Purpose | Effect |
|-------|------------|---------|--------|
| **feature::draft-builds::enabled** | Manual (when ready for build) | Enable Konflux draft builds | Triggers build_rpm CI job |
| **target::latest** | GitLab CI (auto) | Y-stream build target | Builds for latest minor version (rhel-X-main) |
| **target::zstream** | GitLab CI (auto) | Z-stream build target | Builds for specific minor version (rhel-X.Y.0) |
| **target::exception** | Manual | Exception handling | Requires special consultation |

## Brew (Koji) Integration

### Connection Details

**URL:** `https://brewhub.engineering.redhat.com/brewhub`

**Used By:**
- MCP Server (Dist-Git Tools) - For Z-Stream branch creation
- GitLab CI - For official RHEL builds (after draft build passes)

**Authentication:** Kerberos (automatic via system ticket)

### Koji API Operations

| Operation | Purpose | Used By | Returns |
|-----------|---------|---------|---------|
| **listTagged** | Get builds for a specific tag | MCP Dist-Git Tools | List of builds |
| **getBuild** | Get detailed build metadata | MCP Dist-Git Tools | Build info with source ref |

### Z-Stream Branch Creation with Koji

```mermaid
sequenceDiagram
    participant MCP as Dist-Git Tools (MCP)
    participant Koji as Brew/Koji
    participant DistGit as Dist-Git
    participant GitLab

    Note over MCP: Need to create Z-Stream branch<br/>(e.g., rhel-9.8.0)

    MCP->>MCP: Determine candidate tag<br/>(e.g., rhel-9.8.0-candidate)

    MCP->>Koji: listTagged(package, tag, latest=True, inherit=True)
    Note over Koji: Returns latest build<br/>that tag inherited from Y-Stream<br/>or previous Z-Stream
    Koji-->>MCP: Build list (1 item)

    MCP->>Koji: getBuild(build_id)
    Koji-->>MCP: Build metadata with source field

    Note over MCP: Extract git ref from<br/>metadata["source"]<br/>(format: git://...#ref)

    MCP->>DistGit: Push ref to new branch<br/>ssh://pkgs.devel.redhat.com<br/>git push origin ref:refs/heads/branch

    Note over DistGit: Branch created in<br/>internal dist-git

    loop Wait for sync (max 60 min)
        MCP->>GitLab: Check if branch synced<br/>git ls-remote gitlab.com
        GitLab-->>MCP: Branch status

        alt Synced
            MCP-->>MCP: Return success
        else Not yet synced
            MCP->>MCP: Wait 30 seconds
        end
    end

    Note over MCP,GitLab: Z-Stream branch now available<br/>in GitLab for MRs
```

**Why Use Koji?**
- Find the correct base commit for new Z-Stream branches
- Ensures Z-Stream starts from the right build
- Handles cases where higher Z-Streams already exist

## Agent Interaction with Builds

Agents don't directly trigger builds but enable them via MR labels:

```mermaid
flowchart TD
    AGENT[AI Agent]

    AGENT --> CREATE["Create MR<br/>via open_merge_request()"]

    CREATE --> ADD_LABEL_NOTE["Note: MR created with only<br/>jotnar_needs_attention label"]

    ADD_LABEL_NOTE --> MANUAL_LABEL["When ready: Manually add<br/>feature::draft-builds::enabled<br/>via add_merge_request_labels()"]

    MANUAL_LABEL --> CI_DETECT["GitLab CI detects label"]

    CI_DETECT --> BUILD["build_rpm job runs"]

    BUILD --> MONITOR["Agent can monitor via<br/>get_failed_pipeline_jobs_from_merge_request()"]

    MONITOR --> ANALYZE{Pipeline<br/>failed?}

    ANALYZE -->|Yes| COMMENT["Add blocking comment<br/>with failure analysis"]
    ANALYZE -->|No| CONTINUE["Continue workflow"]

    style AGENT fill:#e8f5e9
    style MANUAL_LABEL fill:#fff4e6
    style BUILD fill:#e1f5fe
    style COMMENT fill:#ffcdd2
```

## Z-Stream vs Y-Stream Builds

### Y-Stream (target::latest)

- **Branch:** `rhel-X-main`, `c10s`
- **Purpose:** Latest minor version development
- **Build Target:** Latest release
- **Label:** `target::latest` (auto-applied)

### Z-Stream (target::zstream)

- **Branch:** `rhel-X.Y.0`, `c9s` for RHEL 8/9
- **Purpose:** Bug fixes for specific minor version
- **Build Target:** Specific minor release
- **Label:** `target::zstream` (auto-applied)
- **Special:** May need Z-Stream branch creation first

## Authentication & Configuration

### Brew/Koji

**Authentication:** Kerberos ticket (automatic)

**Initialization:**
```python
principal = await init_kerberos_ticket()
# Returns: username@IPA.REDHAT.COM
```

### Konflux

**Authentication:** Handled by GitLab CI service account

**No direct API access** from agents - all interaction via GitLab CI pipeline

---

**Last Updated:** 2026-03-03
