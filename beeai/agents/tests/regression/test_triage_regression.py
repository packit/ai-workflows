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

    # Verify expected resolution appears in output
    assert expected_resolution in result.stdout.lower(), \
        f"Expected resolution '{expected_resolution}' not found in output"

    # Verify all expected fields appear in output
    for field_name, field_value in expected_fields.items():
        assert str(field_name) in result.stdout and str(field_value) in result.stdout
