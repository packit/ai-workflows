import pytest

from ymir.agents.tests.e2e.backport_agent.evaluation import LLMJudgeEvaluator


@pytest.mark.parametrize(
    "text",
    [
        '{"passed": true, "reasoning": "uses %{version}"}',
        '```json\n{"passed": true, "reasoning": "uses %{version}"}\n```',
        'Checked %{name} and %{?dist}.\n{"passed": true, "reasoning": "uses %{version}"}',
        '{"passed": true, "reasoning": "uses %{version}"}\nNote: see %{release}.',
        'Draft {not json}. {"other": 1} {"passed": true, "reasoning": "uses %{version}"}',
    ],
)
def test_parse_verdict_tolerates_surrounding_text(text):
    assert LLMJudgeEvaluator._parse_verdict(text) == (True, "uses %{version}")


def test_parse_verdict_without_valid_json_raises():
    with pytest.raises(ValueError, match="No valid verdict JSON"):
        LLMJudgeEvaluator._parse_verdict("I think it {passed}.")
