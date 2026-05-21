"""
Unit tests for Jira comment parser
"""

from ymir.agents.golang_rebuild.comment_parser import parse_comment_text, parse_recent_comments


class TestParseCommentText:
    def test_full_comment(self):
        text = """side-tag: rhel-9.4.0-z-gotoolset-stack-gate
release: rhel-9.4.0
commit: test0commit0hash
jiras: RHEL-158645 RHEL-147034 RHEL-146820
message: Rebuilding with golang 1.25.8 for critical security fix"""

        result = parse_comment_text(text)
        assert result is not None
        assert result.side_tag == "rhel-9.4.0-z-gotoolset-stack-gate"
        assert result.release == "rhel-9.4.0"
        assert result.commit == "test0commit0hash"
        assert result.additional_jiras == ["RHEL-158645", "RHEL-147034", "RHEL-146820"]
        assert result.custom_message == "Rebuilding with golang 1.25.8 for critical security fix"

    def test_side_tag_only(self):
        text = "side-tag: rhel-9.4.0-z-gotoolset-stack-gate\nrelease: rhel-9.4.0"
        result = parse_comment_text(text)
        assert result is not None
        assert result.has_side_tag is True
        assert result.side_tag == "rhel-9.4.0-z-gotoolset-stack-gate"
        assert result.release == "rhel-9.4.0"
        assert result.commit is None
        assert result.additional_jiras == []

    def test_commit_only(self):
        text = "commit: test0commit0hash"
        result = parse_comment_text(text)
        assert result is not None
        assert result.has_commit is True
        assert result.commit == "test0commit0hash"
        assert result.has_side_tag is False

    def test_jiras_comma_separated(self):
        text = "jiras: RHEL-111, RHEL-222, RHEL-333"
        result = parse_comment_text(text)
        assert result is not None
        assert set(result.additional_jiras) == {"RHEL-111", "RHEL-222", "RHEL-333"}

    def test_message_only(self):
        text = "message: Custom rebuild reason for security compliance"
        result = parse_comment_text(text)
        assert result is not None
        assert result.custom_message == "Custom rebuild reason for security compliance"

    def test_no_recognized_fields(self):
        text = "This is just a regular comment about the ticket"
        result = parse_comment_text(text)
        assert result is None

    def test_empty_comment(self):
        assert parse_comment_text("") is None
        assert parse_comment_text("   ") is None

    def test_case_insensitive_keys(self):
        text = "Side-Tag: rhel-9.4.0-z-gotoolset-stack-gate\nRelease: rhel-9.4.0"
        result = parse_comment_text(text)
        assert result is not None
        assert result.side_tag == "rhel-9.4.0-z-gotoolset-stack-gate"

    def test_get_rhpkg_args(self):
        text = "side-tag: rhel-9.4.0-z-gotoolset-stack-gate\nrelease: rhel-9.4.0"
        result = parse_comment_text(text)
        assert result.get_rhpkg_args() == ["--release=rhel-9.4.0"]
        assert result.build_target == "rhel-9.4.0-z-gotoolset-stack-gate"

    def test_get_rhpkg_args_no_release(self):
        text = "commit: abc123"
        result = parse_comment_text(text)
        assert result.get_rhpkg_args() == []


class TestParseRecentComments:
    def test_finds_in_most_recent(self):
        comments = [
            {"id": "1", "body": "Regular comment"},
            {"id": "2", "body": "Another regular comment"},
            {"id": "3", "body": "side-tag: rhel-9.4.0-z-gotoolset-stack-gate\nrelease: rhel-9.4.0"},
        ]
        result = parse_recent_comments(comments)
        assert result is not None
        assert result.source_comment_id == "3"
        assert result.side_tag == "rhel-9.4.0-z-gotoolset-stack-gate"

    def test_prefers_newest(self):
        comments = [
            {"id": "1", "body": "commit: old_hash"},
            {"id": "2", "body": "commit: new_hash"},
        ]
        result = parse_recent_comments(comments)
        assert result is not None
        assert result.commit == "new_hash"
        assert result.source_comment_id == "2"

    def test_only_checks_last_three(self):
        comments = [
            {"id": "1", "body": "commit: should_be_ignored"},
            {"id": "2", "body": "Regular comment"},
            {"id": "3", "body": "Regular comment"},
            {"id": "4", "body": "Regular comment"},
            {"id": "5", "body": "Regular comment"},
        ]
        result = parse_recent_comments(comments)
        assert result is None  # comment 1 is too old (>3 from end)

    def test_no_comments(self):
        assert parse_recent_comments([]) is None

    def test_no_matching_comments(self):
        comments = [
            {"id": "1", "body": "Just discussing the ticket"},
            {"id": "2", "body": "Looks good to me"},
        ]
        assert parse_recent_comments(comments) is None
