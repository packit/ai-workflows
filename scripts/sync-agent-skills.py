#!/usr/bin/env python3
"""Pre-push hook: ensure agents_as_skills/ stays in sync with ymir/agents/.

Compares the net diff of the current branch against its remote tracking branch.
Fails the push when a *_agent.py file or its prompt templates were
changed but their corresponding SKILL.md under agents_as_skills/ was not.
Prints the exact command the developer should run to regenerate the skill.

Set SKIP_SKILL_SYNC=1 to bypass this check.
"""

import os
import sys
from pathlib import Path

from git import Repo

AGENTS_DIR = Path("ymir/agents")
PROMPTS_DIR = Path("ymir/agents/prompts")
SKILLS_DIR = Path("agents_as_skills")

REGEN_CMD_TEMPLATE = (
    "claude --model claude-opus-4-6 --effort high"
    "  # any AI agent works; adjust command for your tool"
    ' "Please take a look at the BeeAI workflows implemented in agents'
    " directory. Please convert Workflow in {workflow_file} to an Agent Skill"
    f" (https://agentskills.io/specification) and save it to {SKILLS_DIR}/.\n"
    "Restrictions:\n"
    " - Pay attention to tools used by the workflow and do not omit them\n"
    " - Do not restrict tools that the skill can use\n"
    ' - The skill name must match the directory name (lowercase, hyphens only)"'
)


def skill_name_for(agent_path: Path) -> str | None:
    """Derive the skill directory name from an agent filename.

    Agent filenames use underscores (e.g. preliminary_testing_agent.py) but
    skill directories use hyphens per the agentskills.io spec, so underscores
    are replaced with hyphens.
    """
    if agent_path.suffix != ".py" or not agent_path.stem.endswith("_agent"):
        return None
    return agent_path.stem.removesuffix("_agent").replace("_", "-")


def skill_name_for_prompt(prompt_path: Path) -> str | None:
    """Derive the skill directory name from a prompt template path.

    Prompt templates live under ymir/agents/prompts/<agent>/*.j2.
    The subdirectory name matches the skill name.
    """
    if not prompt_path.is_relative_to(PROMPTS_DIR) or prompt_path.suffix != ".j2":
        return None
    relative = prompt_path.relative_to(PROMPTS_DIR)
    if len(relative.parts) < 2:
        return None
    return relative.parts[0]


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

    affected_skills: dict[str, list[Path]] = {}

    for f in changed_files:
        name = None
        if f.is_relative_to(PROMPTS_DIR) and f.suffix == ".j2":
            name = skill_name_for_prompt(f)
        elif f.is_relative_to(AGENTS_DIR) and f.stem.endswith("_agent"):
            name = skill_name_for(f)

        if name is None:
            continue
        sp = SKILLS_DIR / name / "SKILL.md"
        if sp.exists() and sp not in changed_files:
            affected_skills.setdefault(name, []).append(f)

    if not affected_skills:
        return 0

    print("The following agent workflows were modified without updating their skills:\n")
    for name, sources in sorted(affected_skills.items()):
        skill_file = SKILLS_DIR / name / "SKILL.md"
        for src in sources:
            print(f"  {src} -> {skill_file}")

    print("\nPlease regenerate the skill(s) before pushing:\n")
    for name in sorted(affected_skills):
        workflow_file = AGENTS_DIR / f"{name}_agent.py"
        print(f"  {REGEN_CMD_TEMPLATE.format(workflow_file=workflow_file)}\n")

    print(
        "If the change does not affect the skill (e.g. comments only), bypass with:\n"
        "  SKIP_SKILL_SYNC=1 git push ..."
    )

    return 1


if __name__ == "__main__":
    sys.exit(main())
