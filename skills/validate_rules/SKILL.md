---
description: Quick validation of an AGENTS.md rules file — catch conflicts with non-overridable pipeline behaviors and clearly broken references.
arguments:
  - name: file
    description: "Path to the AGENTS.md file to validate. Defaults to AGENTS.md in the current directory."
    required: false
---

# Validate Rules

Quick check of an AGENTS.md file for clearly wrong things. This is not
a style review — only flag real problems.

## What Ymir agents do

Ymir automates CVE/Jira resolution for RHEL packages with four agents:
triage (investigate the Jira, pick strategy for fixing, search upstream for fix, verify applicability),
backport (cherry-pick patches), rebase (update
to new upstream), rebuild (bump Release for dependency changes).

All agents read the **full** AGENTS.md. Section headings are
organizational — any names work, all agents see everything.

Agents follow guidance on things like: fix strategy preferences, upstream repo
URLs and tag formats, patch naming and specfile styling, files to
ignore, vendor tarball and dependency handling, extra build steps,
CVE classification, and package context.

## Non-overridable behaviors

These are automated and **cannot be overridden** via AGENTS.md:

- Target branch selection (from Jira fix_version)
- CVE applicability/eligibility checks
- Jira labels, transitions, queue dispatch
- Specfile Release bump (automated tool, not rpmdev-bumpspec)
- Commit message `Resolves:` / `Related:` footers (appended automatically, agents are told not to include them)
- MR creation and description

Note: **Changelog entries** and **commit message body** (title +
description) are agent-composed — agents look at existing changelog
style and try to match it. AGENTS.md guidance on these may have
limited effect but is not blocked.

## Steps

1. Read `./AGENTS.md` or the provided path. Stop if missing.

2. For any references to other packages' rules, try to curl them
   (`https://gitlab.com/redhat/centos-stream/rules/<package>/-/raw/main/AGENTS.md`).
   Report reachability.

3. Flag instructions that conflict with a non-overridable behavior.
   Quote the line and note that it won't affect Ymir agents. Do not
   suggest removing it — the file may be used by other tools or humans.

4. If any instructions are outside Ymir's scope (CI, human reviewers,
   other tools), briefly note them — these are not problems.

5. Flag only instructions that are genuinely ambiguous enough to cause
   wrong agent behavior. Do not flag style, wording, or structure.

**If an instruction is not in the non-overridable list, assume agents will
follow it. Never question, caveat, or speculate about whether agents
support something. An empty report is a good outcome.**

## Report

Brief — 1–2 sentences per item, omit empty sections.

- **Summary**: One line.
- **Non-overridable conflicts**: Quote + explain why Ymir won't use it. "None found." if clean.
- **References**: Reachability status. Omit if none.
- **Outside Ymir's scope**: Brief note. Omit if none.
- **Issues**: Only clearly wrong things. "None found." if clean.
