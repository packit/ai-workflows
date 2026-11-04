"""LLM-as-judge evaluator for backport agent e2e tests.

Sends captured backport artifacts to an LLM with structured evaluation
criteria and returns a pass/fail verdict with reasoning.

Usage in tests::

    evaluator = BackportEvaluator()
    verdict = await evaluator.evaluate(artifacts, context)
    assert verdict.passed, verdict.reasoning
"""

import json
import os
import re
from dataclasses import dataclass

from beeai_framework.backend import ChatModel, ChatModelParameters
from beeai_framework.backend.message import UserMessage
from pydantic import BaseModel, Field, ValidationError

from ymir.agents.tests.e2e.backport_agent.artifact_capture import CapturedArtifacts
from ymir.agents.utils import unsupported_model_params


class VerdictSchema(BaseModel):
    """Schema for structured LLM judge output."""

    passed: bool = Field(description="Whether the backport passes all evaluation criteria")
    reasoning: str = Field(description="Detailed explanation for each evaluation criterion")


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

        settings = {}
        dropped_params = unsupported_model_params(model_name)
        if dropped_params:
            settings["drop_params"] = True
            settings["additional_drop_params"] = list(dropped_params)

        # Models without forced tool choice (Opus 5.5) cannot take
        # response_format: litellm implements it for Anthropic models by
        # forcing a hidden structured-output tool call, which those models
        # reject with a 400. For them the schema is communicated through
        # prompt instructions and the raw text is parsed by _parse_verdict.
        self._use_response_format = "tool_choice" not in dropped_params

        self._model = ChatModel.from_name(
            model_name,
            ChatModelParameters(temperature=0.2),
            settings=settings,
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
        if self._use_response_format:
            response = await self._model.run(
                [UserMessage(prompt)],
                response_format=VerdictSchema,
            )
        else:
            prompt += (
                "\n\n## Output format\n\n"
                "Respond with ONLY a JSON object — no markdown fences, no text before or "
                'after — with exactly two keys: "passed" (boolean) and "reasoning" (string).'
            )
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
        """Parse the structured JSON verdict returned by the model."""
        try:
            verdict = VerdictSchema.model_validate_json(text)
            return verdict.passed, verdict.reasoning
        except ValidationError:
            pass
        # Without schema enforcement the model sometimes wraps the JSON in
        # markdown fences or adds prose around it, and that prose may itself
        # contain braces (e.g. RPM macros), so try each candidate object.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                obj, _ = decoder.raw_decode(text, match.start())
                verdict = VerdictSchema.model_validate(obj)
            except (json.JSONDecodeError, ValidationError):
                continue
            return verdict.passed, verdict.reasoning
        raise ValueError(f"No valid verdict JSON found in judge response: {text!r}")


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
   backport? A Release field bump and a new %changelog entry are expected parts
   of an RPM backport commit — deterministic workflow steps add them after the
   agent finishes (the agent's report covers only its own edits), so neither
   counts as an unrelated change. Unrelated changes are modifications to
   existing patches, extra source changes not needed for the fix, or unrelated
   spec changes (e.g. to BuildRequires/Requires).
4. **Completeness**: Was the backport reported as successful? Was an SRPM generated?
   (For a regular backport the SRPM is built before the deterministic Release
   bump, so its filename may carry the pre-bump release value — that is
   expected, not an error. For an inherited backport the Release is bumped
   first, so the SRPM should match the bumped spec.)
{similarity_criterion}

Set `passed` to true only if the backport passes ALL criteria. Provide a brief
explanation for each criterion inside the `reasoning` field.
"""
