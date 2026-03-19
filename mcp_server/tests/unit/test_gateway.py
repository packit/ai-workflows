""" Unit tests for mcp_server/gateway.py """
import pytest

from gateway import _redact


class TestRedactFunction:
    """Test the _redact() function for credential pattern matching."""

    def test_redact_gitlab_pat(self):
        """Test redaction of GitLab Personal Access Token."""
        text = "Token: glpat-aBcDeFgHiJkLmNoPqRsTuVwXyZ1234500000"
        result = _redact(text)
        assert "glpat-" not in result
        assert "[REDACTED]" in result
        assert result == "Token: [REDACTED]"

    def test_redact_anthropic_api_key(self):
        """Test redaction of Anthropic API key."""
        text = "Key: sk-ant-api03-BDbG2jaStLaS_yflKC9aEuAUWsPR8fLGir3rnUYptbp34Vxj80000Pq5azVXQ6LzeXYM--yDbNVZeaY6uAqVXQ-XVDKQgAA"
        result = _redact(text)
        assert "sk-ant-" not in result
        assert "[REDACTED]" in result
        assert result == "Key: [REDACTED]"

    def test_redact_google_api_key(self):
        """Test redaction of Google API key."""
        text = "GOOGLE_API_KEY=AIzaSyCrbXLEWFA45Jn00006XI0DwBF2p7_94Mo"
        result = _redact(text)
        assert "AIzaSy" not in result
        assert "[REDACTED]" in result

    def test_redact_oauth2_token_in_url(self):
        """Test redaction of oauth2 token embedded in URL."""
        text = "https://oauth2:glpat-sometoken123456@gitlab.com/repo"
        result = _redact(text)
        assert "glpat-sometoken123456" not in result
        assert "[REDACTED]" in result
        assert result == "https://[REDACTED]gitlab.com/repo"

    @pytest.mark.parametrize(
        "text",
        [
            "token=abc123def456ghi789jkl012mno345pqr678",
            "key: xyz123abc456def789ghi012jkl345mno678pqr901stu234",
            'password="secret123456789012345678901234567890"',
            "secret = longsecretvalue1234567890abcdefghijklmnop",
            "credential:value1234567890abcdefghijklmnopqrstuvwxyz",
        ],
    )
    def test_redact_generic_token_patterns(self, text: str):
        """Test redaction of generic token/key/password patterns."""
        result = _redact(text)
        assert "[REDACTED]" == result, f"Failed to redact: {text}"

    def test_redact_multiple_credentials(self):
        """Test redaction of multiple credentials in the same text."""
        text = (
            "Using token glpat-abc123456789012345678901234 "
            "and API key sk-ant-api03-xyz789012345678901234567890123456789012345678901234567890123456789012345678901234567890 "
            "to access gitlab.com"
        )
        result = _redact(text)
        assert "glpat-" not in result
        assert "sk-ant-" not in result
        assert result.count("[REDACTED]") == 2

    def test_redact_case_insensitive(self):
        """Test that generic patterns are case-insensitive."""
        test_cases = [
            "TOKEN=abcdefghijklmnopqrstuvwxyz1234567890",
            "Token=abcdefghijklmnopqrstuvwxyz1234567890",
            "PASSWORD=abcdefghijklmnopqrstuvwxyz1234567890",
            "Password=abcdefghijklmnopqrstuvwxyz1234567890",
        ]
        for text in test_cases:
            result = _redact(text)
            assert "[REDACTED]" in result, f"Failed to redact: {text}"

    def test_redact_no_false_positives(self):
        """Test that normal text is not redacted."""
        text = "This is a normal log message with no credentials"
        result = _redact(text)
        assert result == text
        assert "[REDACTED]" not in result

    def test_redact_short_tokens_not_matched(self):
        """Test that short tokens (< 20 chars) are not redacted to avoid false positives."""
        text = "token=short123"
        result = _redact(text)
        # Short tokens should not be redacted
        assert result == text

    def test_redact_empty_string(self):
        """Test redaction of empty string."""
        result = _redact("")
        assert result == ""

    def test_redact_preserves_structure(self):
        """Test that redaction preserves the overall structure of the text."""
        text = "Config: {token: 'glpat-abc123456789012345678901234', url: 'https://example.com'}"
        result = _redact(text)
        assert "[REDACTED]" in result
        assert "url: 'https://example.com'" in result
        assert "glpat-" not in result

    def test_redact_real_world_example_git_url(self):
        """Test redaction in a real-world git command output scenario."""
        text = "Fetching from https://oauth2:glpat-xyz123456789012345678901234@gitlab.com/redhat/rhel/rpms/bash"
        result = _redact(text)
        assert "glpat-" not in result
        assert "[REDACTED]" in result
        assert "gitlab.com/redhat/rhel/rpms/bash" in result

    def test_redact_real_world_example_error_message(self):
        """Test redaction in error message containing a token."""
        text = "Failed to authenticate with token glpat-abc123456789012345678901234: 401 Unauthorized"
        result = _redact(text)
        assert "glpat-" not in result
        assert "[REDACTED]" in result
        assert "401 Unauthorized" in result

    def test_redact_real_world_example_dict_str(self):
        """Test redaction in string representation of a dict containing credentials."""
        text = "{'url': 'https://gitlab.com', 'token': 'glpat-xyz123456789012345678901234'}"
        result = _redact(text)
        assert "glpat-" not in result
        assert "[REDACTED]" in result

    def test_redact_jira_personal_token(self):
        """Test redaction of Jira Personal Access Token (base64-like pattern)."""
        text = "JIRA_TOKEN=NTMxMTc4N00000k5OiNdRf7iO/YZvg7uUwczDkh8iLfR"
        result = _redact(text)
        # This should match the generic token pattern
        assert "[REDACTED]" in result

    def test_redact_multiple_patterns_same_line(self):
        """Test redaction when multiple different credential patterns appear in one line."""
        text = (
            "Authenticating with gitlab token glpat-abc123456789012345678901234 "
            "and anthropic key sk-ant-api03-xyz789012345678901234567890123456789012345678901234567890123456789012345678901234567890 "
            "and google key AIzaSyCrbXLEWFA45Jnl1500000DwBF2p7_94Mo"
        )
        result = _redact(text)
        assert "glpat-" not in result
        assert "sk-ant-" not in result
        assert "AIzaSy" not in result
        assert result.count("[REDACTED]") == 3

    def test_redact_testing_farm_token(self):
        """Test redaction of Testing Farm API token (UUID format)."""
        text = "TESTING_FARM_API_TOKEN=d1f2e3a4-b5c6-7890-abcd-ef1234567890"
        result = _redact(text)
        assert "d1f2e3a4-b5c6-7890-abcd-ef1234567890" not in result
        assert "[REDACTED]" in result

    def test_redact_jira_cloud_token(self):
        """Test redaction of Jira Cloud API token (ATATT3x... pattern)."""
        text = "JIRA_API_TOKEN=ATATT3xFfGF0Z123456788888888YjRhMC1hZGY5MjYxNzQ5OTk"  # pragma: allowlist secret
        result = _redact(text)
        assert "ATATT3x" not in result
        assert "[REDACTED]" in result

    def test_redact_base64_authorization_header(self):
        """Test redaction of Base64 Authorization header."""
        secret = "dXNlcm44444444444444444xMjM0NTY3ODkw"  # pragma: allowlist secret
        text = f"Authorization: Basic {secret}"
        result = _redact(text)
        assert secret not in result
        assert "[REDACTED]" in result
        assert result == "Authorization: [REDACTED]"

    def test_redact_does_not_affect_safe_content(self):
        """Test that redaction doesn't affect legitimate non-credential content."""
        safe_texts = [
            "Processing package bash version 5.2.15",
            "Build succeeded in 42 seconds",
            "Merging PR #12345",
            "Error: file not found",
            "token is missing",  # Missing actual value
            "key=",  # Empty value
            "password:",  # No actual password
        ]
        for text in safe_texts:
            result = _redact(text)
            assert result == text, f"Safe text was incorrectly modified: {text}"
            assert "[REDACTED]" not in result
