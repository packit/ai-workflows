#!/usr/bin/env python3
"""Pre-push hook: ensure agents_as_skills/ stays in sync with ymir/agents/.

Compares the net diff of the current branch against its remote tracking branch.
Fails the push when a *_agent.py file was changed but its corresponding
SKILL.md under agents_as_skills/ was not.  Prints the exact claude command
the developer should run to regenerate the skill.

Set SKIP_SKILL_SYNC=1 to bypass this check.
"""

import os
import sys
from pathlib import Path

from git import Repo

AGENTS_DIR = Path("ymir/agents")
SKILLS_DIR = Path("agents_as_skills")

CLAUDE_CMD_TEMPLATE = (
    "claude --model claude-sonnet-4-6 --effort high"
    ' "Please take a look at the BeeAI workflows implemented in agents'
    " directory. Please convert Workflow in {workflow_file} to Claude skill and"
    f" save that skill to {SKILLS_DIR} directory.\n"
    "Restrictions:\n"
    " - Pay attention to tools used by the workflow and do not omit them\n"
    " - Do not restrict tools that the skill can use\n"
    ' - Specify arguments the skill uses as an input"'
)


def skill_name_for(agent_path: Path) -> str | None:
    """Derive the skill directory name from an agent filename."""
    if agent_path.suffix != ".py" or not agent_path.stem.endswith("_agent"):
        return None
    return agent_path.stem.removesuffix("_agent")


def get_repo() -> Repo:
    """Return the Repo object for the current working directory."""
    return Repo(".", search_parent_directories=True)


def get_upstream(repo: Repo) -> str:
    """Return the remote tracking branch, falling back to origin/main."""
    try:
        tracking = repo.active_branch.tracking_branch()
        if tracking is None:
            return "origin/main"
        return str(tracking)
    except (TypeError, ValueError):
        return "origin/main"


def get_branch_diff(repo: Repo, upstream: str) -> set[Path]:
    """Return the set of files changed between upstream and HEAD.

    Uses the merge-base of *upstream* and HEAD (three-dot semantics).
    """
    merge_bases = repo.merge_base(upstream, "HEAD")
    if not merge_bases:
        return set()

    diffs = merge_bases[0].diff(repo.head.commit)
    paths: set[Path] = set()
    for d in diffs:
        if d.a_path:
            paths.add(Path(d.a_path))
        if d.b_path:
            paths.add(Path(d.b_path))
    return paths


def main() -> int:
    if os.environ.get("SKIP_SKILL_SYNC"):
        return 0

    repo = get_repo()
    upstream = get_upstream(repo)
    changed_files = get_branch_diff(repo, upstream)

    missing = [
        (f, sp)
        for f in changed_files
        if f.is_relative_to(AGENTS_DIR)
        and f.stem.endswith("_agent")
        and (n := skill_name_for(f)) is not None
        and (sp := SKILLS_DIR / n / "SKILL.md").exists()
        and sp not in changed_files
    ]

    if not missing:
        return 0

    print("The following agent workflows were modified without updating their skills:\n")
    for agent_file, skill_file in missing:
        print(f"  {agent_file} -> {skill_file}")

    print("\nPlease regenerate the skill(s) before pushing:\n")
    for agent_file, _ in missing:
        print(f"  {CLAUDE_CMD_TEMPLATE.format(workflow_file=agent_file)}\n")

    print(
        "If the change does not affect the skill (e.g. comments only), bypass with:\n"
        "  SKIP_SKILL_SYNC=1 git push ..."
    )

    return 1


if __name__ == "__main__":
    sys.exit(main())
