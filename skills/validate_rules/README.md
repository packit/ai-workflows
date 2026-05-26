# Validate Rules Skill

Validates your `AGENTS.md` rules file before you merge it — checks for
format issues, instructions that conflict with non-overridable agent
behaviors, and suggests improvements.

No MCP tools or special setup required — works with any Claude Code or
Cursor installation out of the box.

## Installation

### Claude Code

```bash
mkdir -p ~/.claude/skills/validate_rules
curl -fsSL https://raw.githubusercontent.com/packit/ai-workflows/main/skills/validate_rules/SKILL.md \
  -o ~/.claude/skills/validate_rules/SKILL.md
```

### Cursor

Copy `SKILL.md` to your Cursor rules directory, or reference it
directly in your `.cursorrules` file.

## Usage

Clone your package's rules repository and run the skill from within it:

```bash
cd ~/rules/my-package
claude /validate_rules
```

This reads `./AGENTS.md` by default and produces a validation report.

To validate a file at a different path, invoke the skill and specify
the path in the prompt:

```bash
claude "/validate_rules validate the file at path/to/AGENTS.md"
```

## What it checks

- **Non-overridable conflicts** — instructions that try to control
  behaviors the pipeline handles automatically (target branch, Jira
  labels, changelog entries, etc.)
- **Cross-references** — links to other files or packages' rules;
  flags them for you to verify
- **Format and clarity** — vague instructions, redundant rules, and
  structural consistency
