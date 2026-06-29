from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import git

from ymir.agents.tests.e2e.backport_agent.evaluation import LLMJudgeEvaluator

if TYPE_CHECKING:
    from ymir.agents.backport_agent import BackportState

logger = logging.getLogger(__name__)


@dataclass
class ConsolidationArtifacts:
    """Artifacts extracted from a finished consolidation workflow and its source backports."""

    output_dir: Path
    commit_diff: str | None = None
    spec_content: str | None = None
    consolidated_patches: dict[str, str] = field(default_factory=dict)
    backport_patches: dict[str, dict[str, str]] = field(default_factory=dict)
    result_json: dict | None = None


def capture_consolidation_artifacts(
    consolidation_state,
    backport_states: dict[str, BackportState],
    backport_issues: list[dict],
    output_dir: Path,
) -> ConsolidationArtifacts:
    """Extract artifacts from a finished consolidation and its source backports.

    Args:
        consolidation_state: Finished ``ConsolidationState``.
        backport_states: Map of jira_issue → finished ``BackportState``.
        backport_issues: The fixture ``backport_issues`` configs.
        output_dir: Root directory for persisted artifacts.

    Returns:
        ``ConsolidationArtifacts`` with all extracted data.
    """
    issue_dir = output_dir / "consolidation"
    issue_dir.mkdir(parents=True, exist_ok=True)

    artifacts = ConsolidationArtifacts(output_dir=issue_dir)

    clone = consolidation_state.local_clone
    if clone and Path(clone).is_dir():
        try:
            repo = git.Repo(str(clone))
            diff = repo.git.diff("HEAD~1", "HEAD")
            if diff.strip():
                artifacts.commit_diff = diff
                (issue_dir / "commit.diff").write_text(diff)
        except Exception as exc:
            logger.warning("Could not extract git diff: %s", exc)

        spec_path = Path(clone) / f"{consolidation_state.package}.spec"
        if spec_path.is_file():
            content = spec_path.read_text()
            artifacts.spec_content = content
            (issue_dir / "spec_file.spec").write_text(content)

        patches_dir = issue_dir / "consolidated_patches"
        patches_dir.mkdir(exist_ok=True)
        for ext in ("*.patch", "*.diff"):
            for p in Path(clone).glob(ext):
                artifacts.consolidated_patches[p.name] = p.read_text()
                (patches_dir / p.name).write_text(p.read_text())

    if consolidation_state.consolidation_result:
        result_data = json.loads(consolidation_state.consolidation_result.model_dump_json())
        artifacts.result_json = result_data
        (issue_dir / "consolidation_result.json").write_text(json.dumps(result_data, indent=2))

    for issue_cfg in backport_issues:
        jira = issue_cfg["jira_issue"]
        bp_state = backport_states.get(jira)
        if not bp_state or not bp_state.local_clone:
            continue
        bp_patches = {}
        try:
            bp_repo = git.Repo(str(bp_state.local_clone))
            changed = bp_repo.git.diff("HEAD~1", "HEAD", "--name-only").splitlines()
            for f in changed:
                if f.endswith((".patch", ".diff")):
                    fp = Path(bp_state.local_clone) / f
                    if fp.is_file():
                        bp_patches[f] = fp.read_text()
        except Exception as exc:
            logger.warning("Could not extract backport patches for %s: %s", jira, exc)
        artifacts.backport_patches[jira] = bp_patches

    bp_dir = issue_dir / "backport_patches"
    bp_dir.mkdir(exist_ok=True)
    for jira, patches in artifacts.backport_patches.items():
        jira_dir = bp_dir / jira
        jira_dir.mkdir(exist_ok=True)
        for name, content in patches.items():
            (jira_dir / name).write_text(content)

    logger.info("Captured consolidation artifacts in %s", issue_dir)
    return artifacts


class ConsolidationEvaluator(LLMJudgeEvaluator):
    """LLM judge that verifies consolidated patches still fix all original issues."""

    def build_prompt(self, artifacts: ConsolidationArtifacts, context: dict) -> str:
        diff_section = (
            f"## Git diff of the consolidated commit\n\n```diff\n{artifacts.commit_diff}\n```"
            if artifacts.commit_diff
            else "## Git diff\n\n(not available)"
        )

        spec_section = (
            f"## Consolidated spec file\n\n```spec\n{artifacts.spec_content}\n```"
            if artifacts.spec_content
            else "## Spec file\n\n(not available)"
        )

        consolidated_section = ""
        if artifacts.consolidated_patches:
            consolidated_section = "## Patch files in the consolidated branch\n\n"
            for name, content in artifacts.consolidated_patches.items():
                preview = content[:3000] + "..." if len(content) > 3000 else content
                consolidated_section += f"### {name}\n\n```diff\n{preview}\n```\n\n"

        backport_section = ""
        if artifacts.backport_patches:
            backport_section = "## Original backport patches (one per issue)\n\n"
            for jira, patches in artifacts.backport_patches.items():
                cve = context.get("issues", {}).get(jira, {}).get("cve_id", "")
                header = f"{jira}"
                if cve:
                    header += f" ({cve})"
                backport_section += f"### {header}\n\n"
                for name, content in patches.items():
                    preview = content[:3000] + "..." if len(content) > 3000 else content
                    backport_section += f"#### {name}\n\n```diff\n{preview}\n```\n\n"

        result_section = ""
        if artifacts.result_json:
            result_section = (
                "## Consolidation result (agent output)\n\n"
                f"```json\n{json.dumps(artifacts.result_json, indent=2)}\n```"
            )

        package = context.get("package", "unknown")
        issues_info = context.get("issues", {})
        issues_lines = []
        for jira, info in issues_info.items():
            cve = info.get("cve_id", "")
            patches_list = ", ".join(info.get("upstream_patches", []))
            issues_lines.append(f"- **{jira}** (CVE: {cve or 'N/A'}): upstream patches: {patches_list}")
        issues_text = "\n".join(issues_lines) if issues_lines else "(none)"

        return f"""You are a senior RPM packaging reviewer evaluating an automated MR consolidation.

Two separate backport merge requests were created for the same package to fix different CVEs.
An automated agent has consolidated them into a single changeset. Your task is to verify
that the consolidation preserved the fixes from both original backports.

## Task context

- **Package**: {package}
- **Original issues**:
{issues_text}

{diff_section}

{spec_section}

{consolidated_section}

{backport_section}

{result_section}

## Evaluation criteria

Evaluate the consolidation on these criteria and explain your reasoning for each:

1. **Patch completeness**: Are all patch files from both original backports present in the
   consolidated branch? None should be missing or dropped.
2. **Patch integrity and adaptation**: Are patch files correct for sequential application?
   When two patches touch overlapping source files, the later patch MUST be adapted so that
   its context lines match the post-first-patch source code. This means the later patch may
   legitimately differ from the original standalone backport — for example, context lines
   may reference updated API signatures, and sub-patches that only served as API adaptation
   shims for standalone application may be dropped if the earlier patch already provides that
   API. Judge this criterion by whether the adapted patch preserves the functional security
   fix, NOT by whether it is byte-identical to the original.
3. **CVE coverage**: Does the consolidated changeset still address ALL original CVEs/issues?
   Each original fix must remain functionally intact.
4. **Spec file correctness**: Does the spec file contain Patch tags for all patches? Are they
   applied in the %prep section with correct arguments? Were pre-existing patches left
   untouched?
5. **No regressions**: Were any pre-existing patches disturbed? Is the Release field handled
   correctly (single consolidated bump or per-commit, depending on strategy)?
6. **No unrelated changes**: Does the consolidated diff introduce anything beyond combining
   the two backport MRs (e.g., stray whitespace, unrelated files)?

Set `passed` to true only if the consolidation passes ALL criteria. Provide a brief
explanation for each criterion inside the `reasoning` field.
"""
