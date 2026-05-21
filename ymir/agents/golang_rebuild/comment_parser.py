"""
Parse build instructions from Jira comments.

Engineers add a comment to the component ticket before applying the
golang-rebuild-queue label. The agent reads the 3 most recent comments
and extracts build instructions.

Supported fields (all optional, one per line, case-insensitive keys):
    side-tag: rhel-9.4.0-z-gotoolset-stack-gate
    release: rhel-9.4.0
    commit: <commit-hash>
    jiras: RHEL-158645 RHEL-147034 RHEL-146820
    message: Rebuilding with golang 1.25.8 for critical security fix

If no structured comment is found, the agent falls back to default behavior.
"""

import logging
import re

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Fields we look for in comments (case-insensitive)
KNOWN_FIELDS = {"side-tag", "release", "commit", "jiras", "message"}


class BuildInstructions(BaseModel):
    """Parsed build instructions from a Jira comment."""

    side_tag: str | None = Field(
        default=None, description="Side-tag target (e.g., rhel-9.4.0-z-gotoolset-stack-gate)"
    )
    release: str | None = Field(default=None, description="--release flag for rhpkg (e.g., rhel-9.4.0)")
    commit: str | None = Field(default=None, description="Commit hash for %global commit0 update")
    additional_jiras: list[str] = Field(
        default_factory=list, description="Additional Jira IDs for changelog/commit"
    )
    custom_message: str | None = Field(default=None, description="Custom changelog/commit message")
    source_comment_id: str | None = Field(
        default=None, description="ID of the comment these instructions came from"
    )

    @property
    def has_side_tag(self) -> bool:
        return self.side_tag is not None

    @property
    def has_commit(self) -> bool:
        return self.commit is not None

    @property
    def build_target(self) -> str | None:
        """Return side-tag as build target if set, else None (use default)."""
        return self.side_tag

    def get_rhpkg_args(self) -> list[str]:
        """Get extra rhpkg arguments for --release flag."""
        if self.release:
            return [f"--release={self.release}"]
        return []


def parse_comment_text(text: str) -> BuildInstructions | None:
    """
    Parse a single comment for build instructions.

    Looks for key: value lines. A comment is considered to have build
    instructions if it contains at least one recognized field.

    Args:
        text: Comment body text

    Returns:
        BuildInstructions if any fields found, None otherwise
    """
    if not text or not text.strip():
        return None

    instructions = {}
    lines = text.strip().splitlines()

    for line in lines:
        line = line.strip()
        if not line or ":" not in line:
            continue

        # Split on first colon only
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if not value:
            continue

        if key == "side-tag":
            instructions["side_tag"] = value
        elif key == "release":
            instructions["release"] = value
        elif key == "commit":
            instructions["commit"] = value
        elif key == "jiras":
            # Parse space or comma separated Jira keys
            jira_keys = re.findall(r"[A-Z]+-\d+", value.upper())
            if jira_keys:
                instructions["additional_jiras"] = jira_keys
        elif key == "message":
            instructions["custom_message"] = value

    if not instructions:
        return None

    logger.info(f"Parsed build instructions: {list(instructions.keys())}")
    return BuildInstructions(**instructions)


def parse_recent_comments(comments: list[dict]) -> BuildInstructions | None:
    """
    Parse the 3 most recent comments for build instructions.

    Checks comments in reverse chronological order (newest first).
    Returns the first comment that contains recognized fields.

    Args:
        comments: List of comment dicts with "body" and optionally "id" keys.
                  Should be ordered oldest-first (Jira default).

    Returns:
        BuildInstructions from the most recent matching comment, or None
    """
    # Take last 3 comments (most recent), check newest first
    recent = comments[-3:] if len(comments) > 3 else comments
    recent = list(reversed(recent))

    for comment in recent:
        body = comment.get("body", "")
        result = parse_comment_text(body)
        if result:
            result.source_comment_id = comment.get("id")
            logger.info(f"Found build instructions in comment {result.source_comment_id}")
            return result

    logger.info("No build instructions found in recent comments")
    return None
