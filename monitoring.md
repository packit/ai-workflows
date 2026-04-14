# Ymir Agent Monitoring and Performance Review

## Overview

Agent performance may change due to model updates, infrastructure changes, or evolving issue types. This document outlines monitoring processes to ensure quality and enable continuous improvement.

## Weekly Review Role: Skald

Weekly rotating role ([definition](https://github.com/packit/agile/issues/972)) responsible for:
- Conducting weekly review sessions
- Monitoring agent activities and identifying patterns
- Triaging escalated issues
- Preparing weekly status reports

## What Gets Logged and Reviewed

### Observability Platform
- **Tool**: Phoenix  link TBD to new cluster
- **Retention**: 2 weeks
- **Filter by**: Jira issue key in metadata or input.value

### Agent Workflows Tracked
- **Triage**: Issue analysis, decisions, patch validation, Jira updates
- **Rebase**: Version mapping, specfile updates, builds
- **Backport**: Patch application, merge conflicts, builds, tests
- **MR**: Creation, iterations, CI monitoring

### Weekly Review Artifacts
The team primarily tracks these items using Jira dashboards:
- Newly triaged issues and CVEs
- Issues with `ymir_needs_attention` or `ymir_*_errored` labels
- Z-stream issues in current batches
- MR quality, correctness, and completeness
- Agent decision accuracy (triage, patch selection, severity)

**Dashboards**:
- [Review dashboard](https://issues.redhat.com/secure/Dashboard.jspa?selectPageId=12389898)
- [Post-merge dashboard](https://issues.redhat.com/secure/Dashboard.jspa?selectPageId=12390403)

## Anomaly Detection

### Automated Signals
- **Labels**: `ymir_needs_attention`, `ymir_*_errored`, `ymir_cant_do`
- **Build/Test**: CI failures, ROG gating failures, test regressions

### Manual Review Focus
- **Quality**: Incorrect patches, incomplete fixes, spec file errors, backwards compatibility issues
- **Patterns**: Repeated failures by package type or upstream source, declining success rates
- **Edge Cases**: RHIVOS/FuSa packages, modules, embargoed CVEs

## Escalation Process

| Level | Channel | Use For |
|-------|---------|---------|
| **1. Team** | `#forum-jötnar-package-automation` | Questions, feedback, label reviews |
| **2. Jira** | Packit project, jotnar component | Unresolved issues, bugs, feature requests |
| **3. Leadership** | Contact team managers directly | Critical CVEs, VP escalations, systemic failures |
| **4. Anonymous** | [Feedback form](https://docs.google.com/forms/d/1bqPhabn5M_D6qBNW0nAoucdlbU0TNl48AJmgptJQ8hI/viewform) | Sensitive concerns |

### Special Cases
- **RHIVOS/FuSa** (24 packages): Maintainer approval required before merge
- **Embargoed CVEs**: NOT handled by agents; escalate if urgent

## Key Performance Metrics

### 1. Automation Ratio
Ratio of MRs merged authored by automation versus human maintainers. A long-term increasing trend indicates successful automation adoption. Stagnation or decreasing trends signal problems with workflows or agent capabilities.

### 2. Total Workflows Triggered
Number of maintainer tasks automated, broken down by workflow type (triage, rebase, backport, build, merge). Consistent increase demonstrates growing automation value. Declining usage in specific workflow types indicates need for evaluation and improvement.

## Updating Prompts and Agent Behavior

### Weekly Review Session
- **Who**: Full team, led by Skald
- **When**: Weekly, 1-2 hours
- **Agenda**: Metrics, labels, CVEs, error patterns, prioritization
- **Output**: Issue priorities, prompt recommendations, backlog items, status report input

### Continuous Improvement Process

The team reviews agent results during weekly sessions, identifies failing use cases from Phoenix traces and Jira issues, applies new prompts to address the issues, and reruns agents locally to verify the fixes before re-deployment.

## Quick Reference

### Common Labels
| Label | Meaning | Action |
|-------|---------|--------|
| `ymir_needs_attention` | Requires team review | Weekly priority |
| `ymir_*_errored` | Workflow failed | Review if >3 attempts |
| `ymir_cant_do` | Agent cannot handle | Human takeover |
| `ymir_fusa` | RHIVOS FuSa package | Maintainer approval |

### Links
- [Skald Role](https://github.com/packit/agile/issues/972)
- [Phoenix Traces](https://phoenix-jotnar-ymir--jotnar-ymir.apps.cyborg.fio9.p1.openshiftapps.com/projects/UHJvamVjdDox/spans)
- [Review Dashboard](https://issues.redhat.com/secure/Dashboard.jspa?selectPageId=12389898)
- [Slack](https://redhat.enterprise.slack.com/archives/C095699FLMR)
