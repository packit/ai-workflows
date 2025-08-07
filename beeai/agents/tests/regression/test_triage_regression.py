"""Regression tests for triage agent using pytest."""

import subprocess
import pytest
from pathlib import Path

# Test cases: (jira_issue, expected_resolution, expected_fields)
# Each test case checks that the agent produces the expected resolution
# and that specific fields appear in the output
REGRESSION_CASES = [
    ("RHEL-73779", "backport", {"patch_url": "https://github.com/systemd/systemd/commit/d8113a2863c460d9327ccb03b888c870dd8cfb17.patch"}),
]

@pytest.mark.parametrize("jira_issue,expected_resolution,expected_fields", REGRESSION_CASES)
def test_triage_regression(jira_issue, expected_resolution, expected_fields):
    """Test that triage agent produces expected resolution and output fields."""
    result = subprocess.run([
        "make", f"JIRA_ISSUE={jira_issue}", "run-triage-agent-standalone"
    ], cwd=Path(__file__).parent.parent.parent.parent, capture_output=True, text=True)

    # Verify agent ran successfully
    assert result.returncode == 0, f"Triage agent failed: {result.stderr}"

    # The agent logs the output as a JSON string to stderr. We need to extract and parse it.
    output_json_str = None
    # The log format from triage_agent.py is "Direct run completed: { ...JSON... }"
    match = re.search(r"Direct run completed: (\{.*\})", result.stderr, re.DOTALL)
    if match:
        output_json_str = match.group(1)

    assert output_json_str, f"Could not find JSON output in agent logs:\n{result.stderr}"
    output_data = json.loads(output_json_str)

    # Verify expected resolution
    assert output_data.get("resolution") == expected_resolution, \
        f"Expected resolution '{expected_resolution}', but got '{output_data.get('resolution')}'"

    # Verify all expected fields appear in the 'data' part of the output
    data_part = output_data.get("data", {})
    for field_name, field_value in expected_fields.items():
        assert data_part.get(field_name) == field_value, \
            f"Field '{field_name}': expected '{field_value}', but got '{data_part.get(field_name)}'"
