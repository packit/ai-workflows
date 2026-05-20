"""LLM-as-judge evaluator for backport agent e2e tests.

Sends captured backport artifacts to an LLM with structured evaluation
criteria and returns a pass/fail verdict with reasoning.

Usage in tests::

    evaluator = BackportEvaluator()
    verdict = await evaluator.evaluate(artifacts, context)
    assert verdict.passed, verdict.reasoning
"""

import json
import logging
import os
from dataclasses import dataclass

from beeai_framework.backend import ChatModel, ChatModelParameters
from beeai_framework.backend.message import UserMessage

from ymir.agents.tests.e2e.backport_agent.artifact_capture import CapturedArtifacts

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    """Result of an LLM judge evaluation."""

    passed: bool
    reasoning: str
    raw_response: str


class LLMJudgeEvaluator:
    """Base class for LLM-as-judge evaluations.

    Subclasses override ``build_prompt`` to supply domain-specific
    evaluation criteria.  The judge model is configured via the
    ``LLM_JUDGE_MODEL`` env var (defaults to the same ``CHAT_MODEL``).
    """

    def __init__(self) -> None:
        model_name = os.environ.get("LLM_JUDGE_MODEL", os.environ.get("CHAT_MODEL", ""))
        if not model_name:
            raise RuntimeError("LLM_JUDGE_MODEL or CHAT_MODEL must be set to use the LLM judge")
        self._model = ChatModel.from_name(
            model_name,
            ChatModelParameters(temperature=0.2),
        )

    def build_prompt(self, artifacts: CapturedArtifacts, context: dict) -> str:
        """Build the evaluation prompt.  Override in subclasses."""
        raise NotImplementedError

    async def evaluate(self, artifacts: CapturedArtifacts, context: dict) -> Verdict:
        """Run the LLM judge and parse the verdict.

        Args:
            artifacts: Captured workflow artifacts.
            context: Test-case-specific context (expected values, issue
                metadata, etc.).

        Returns:
            A ``Verdict`` with ``passed``, ``reasoning``, and the raw
            LLM response.
        """
        prompt = self.build_prompt(artifacts, context)
        response = await self._model.run([UserMessage(prompt)])
        raw = response.get_text_content()

        passed, reasoning = self._parse_verdict(raw)
        verdict = Verdict(passed=passed, reasoning=reasoning, raw_response=raw)

        verdict_path = artifacts.output_dir / "judge_verdict.json"
        verdict_path.write_text(
            json.dumps(
                {"passed": passed, "reasoning": reasoning},
                indent=2,
            )
        )

        return verdict

    @staticmethod
    def _parse_verdict(text: str) -> tuple[bool, str]:
        """Extract PASS/FAIL and reasoning from the judge response.

        Expects the LLM to include ``VERDICT: PASS`` or ``VERDICT: FAIL``
        somewhere in its response.
        """
        text_upper = text.upper()
        if "VERDICT: PASS" in text_upper:
            passed = True
        elif "VERDICT: FAIL" in text_upper:
            passed = False
        else:
            passed = False
            text = f"[Could not parse verdict from response]\n\n{text}"
        return passed, text


class BackportEvaluator(LLMJudgeEvaluator):
    """Evaluator for backport agent artifacts."""

    def build_prompt(self, artifacts: CapturedArtifacts, context: dict) -> str:
        diff_section = (
            f"## Git diff of the backport commit\n\n```diff\n{artifacts.commit_diff}\n```"
            if artifacts.commit_diff
            else "## Git diff\n\n(not available)"
        )

        spec_section = (
            f"## Modified spec file\n\n```spec\n{artifacts.spec_content}\n```"
            if artifacts.spec_content
            else "## Spec file\n\n(not available)"
        )

        patches_section = ""
        if artifacts.patch_files:
            patches_section = "## New patch files\n\n"
            for name, content in artifacts.patch_files.items():
                preview = content[:3000] + "..." if len(content) > 3000 else content
                patches_section += f"### {name}\n\n```diff\n{preview}\n```\n\n"

        result_section = ""
        if artifacts.result_json:
            result_section = (
                "## Backport result (agent output)\n\n"
                f"```json\n{json.dumps(artifacts.result_json, indent=2)}\n```"
            )

        jira_issue = context.get("jira_issue", "unknown")
        cve_id = context.get("cve_id", "")
        package = context.get("package", "unknown")
        upstream_patches = context.get("upstream_patches", [])
        patches_list = "\n".join(f"  - {u}" for u in upstream_patches)

        reference_patch = context.get("reference_patch")
        if reference_patch:
            reference_section = (
                f"## Reference patch (known-good production fix)\n\n```diff\n{reference_patch}\n```"
            )
            similarity_criterion = (
                "5. **Similarity to reference patch**: A known-good production patch is provided above.\n"
                "   Compare the agent's generated patch against it. The core logic (changed lines,\n"
                "   added NULL checks, modified conditions, etc.) should be functionally equivalent.\n"
                "   Minor differences are acceptable: different context line counts, different patch\n"
                "   headers/metadata, different file path strip levels, or whitespace variations.\n"
                "   What matters is that the same source lines are changed in the same way.\n"
                "6. **File scope**: The agent's patch must only modify the same source files as\n"
                "   the reference patch. If the reference patch does not touch CHANGELOG,\n"
                "   documentation, copyright notices, or other non-code files, the agent's\n"
                "   patch must not touch them either. Any extra files are a FAIL."
            )
        else:
            reference_section = ""
            similarity_criterion = ""

        return f"""You are a senior RPM packaging reviewer evaluating an automated backport.

## Task context

- **Jira issue**: {jira_issue}
- **CVE**: {cve_id or "(none)"}
- **Package**: {package}
- **Upstream patches**:
{patches_list}

{diff_section}

{spec_section}

{patches_section}

{result_section}

{reference_section}

## Evaluation criteria

Evaluate the backport on these criteria and explain your reasoning for each:

1. **Patch correctness**: Does the generated patch file address the Jira issue / CVE?
   Does it contain the essential logic of the upstream fix?
2. **Spec file correctness**: Was a new Patch tag added correctly? Is it applied in
   the %prep section with the correct -p argument? Were existing patches left untouched?
3. **No unrelated changes**: Does the diff introduce any changes unrelated to the
   backport (e.g., changelog modifications, Release field changes, unrelated patches)?
4. **Completeness**: Was the backport reported as successful? Was an SRPM generated?
{similarity_criterion}

## Output format

End your response with exactly one of:

    VERDICT: PASS
    VERDICT: FAIL

Before the verdict, provide a brief explanation for each criterion.
"""
