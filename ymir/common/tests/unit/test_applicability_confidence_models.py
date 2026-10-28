import pytest
from pydantic import ValidationError

from ymir.common.models import (
    ApplicabilityResult,
    AssessedApplicabilityResult,
    BackportData,
    ConfidenceAssessment,
    NotAffectedData,
    Resolution,
    TriageOutputSchema,
)


@pytest.mark.parametrize("level", ["high", "medium", "low"])
def test_confidence_round_trip(level):
    assessment = ConfidenceAssessment(level=level, reason="Inspected target source and build features")
    assert ConfidenceAssessment.model_validate_json(assessment.model_dump_json()) == assessment


@pytest.mark.parametrize("level", ["certain", "HIGH", 1, None])
def test_invalid_confidence_level(level):
    with pytest.raises(ValidationError):
        ConfidenceAssessment(level=level, reason="Evidence")


@pytest.mark.parametrize("reason", ["", " ", "\n\t"])
def test_confidence_requires_a_substantive_reason(reason):
    with pytest.raises(ValidationError):
        ConfidenceAssessment(level="high", reason=reason)


def test_new_applicability_responses_require_confidence():
    previous = {"is_affected": True, "explanation": "Existing recorded assessment"}
    assert ApplicabilityResult.model_validate(previous).confidence is None
    with pytest.raises(ValidationError):
        AssessedApplicabilityResult.model_validate(previous)


@pytest.mark.parametrize(
    "resolution, data",
    [
        (Resolution.NOT_AFFECTED, NotAffectedData(jira_issue="RHEL-1", explanation="Component absent")),
        (
            Resolution.BACKPORT,
            BackportData(package="bash", jira_issue="RHEL-1", patch_urls=[], justification="Fix verified"),
        ),
    ],
)
def test_comments_and_results_preserve_confidence(resolution, data):
    output = TriageOutputSchema(
        resolution=resolution,
        data=data,
        confidence=ConfidenceAssessment(level="medium", reason="Runtime build features unverified"),
    )
    restored = TriageOutputSchema.model_validate_json(output.model_dump_json())
    assert "*Confidence*: medium" in restored.format_for_comment()
    assert "*Confidence Reason*: Runtime build features unverified" in restored.format_for_comment()
    assert restored.resolution == resolution


def test_unassessed_results_have_no_rating():
    output = TriageOutputSchema(
        resolution=Resolution.NOT_AFFECTED,
        data=NotAffectedData(jira_issue="RHEL-1", explanation="Existing recorded result"),
    )
    assert output.confidence is None
    assert "Confidence" not in output.format_for_comment()
