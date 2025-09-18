# AI Workflows Platform

An AI automation platform for Red Hat engineering workflows, primarily powered by the **BeeAI framework**. This repository provides automated solutions for RHEL/CentOS package management, issue triage, and development workflows.

## BeeAI tooling

**The main and actively maintained AI automation tooling is in the [BeeAI directory](./beeai/)**.

👉 For setup instructions, usage, and documentation, please see [beeai/README.md](./beeai/README.md)

BeeAI provides automated AI agents for RHEL engineering workflows, including issue triage, package management, and testing integration.

👉 For detailed capabilities, architecture, and workflows, see [beeai/README-agents.md](./beeai/README-agents.md)

👉 For complete setup and usage instructions, see [beeai/README.md](./beeai/README.md)


## 📁 Repository Structure

```
ai-workflows/
├── beeai/                    # 🚀 BeeAI Framework (PRIMARY TOOLING)
│   ├── agents/               # Specialized AI agents (triage, rebase, backport)
│   ├── mcp_server/           # MCP server implementations
│   ├── supervisor/           # Workflow orchestration
│   ├── openshift/            # Production deployment configs
│   └── ... (see beeai/README.md for details)
├── goose/                    # ⚠️ Legacy Goose AI (unmaintained)
│   ├── recipes/              # Historical automation recipes
│   └── ... (preserved for reference)
├── scripts/                  # Utility scripts and tools
├── templates/                # Shared configuration templates
└── testing-farm-sse-bridge/ # Testing Farm integration bridge
```

## 🤝 Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines.

**Development Focus**: All new development should target the [BeeAI framework](./beeai/). The Goose components are preserved for reference but are not actively maintained.

**Merging Policy**: Prefer rebase-merging over merge commits unless preserving branch history is necessary.
