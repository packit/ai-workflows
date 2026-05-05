"""
RPM Spec File Parser and Modifier

Handles parsing RPM spec files, bumping release versions,
and adding changelog entries for golang rebuilds.
"""

import logging
import re
from pathlib import Path

from ymir.agents.golang_rebuild.utils import format_cve_list, format_date_for_changelog, format_jira_list

logger = logging.getLogger(__name__)


class SpecFile:
    """
    RPM Spec File parser and modifier.

    Handles operations specific to golang rebuild workflow:
    - Bump release version (e.g., 3%{?dist} -> 3%{?dist}.1)
    - Add changelog entry
    - Extract metadata (name, version, release, epoch, NVR)
    """

    def __init__(self, spec_path: str | Path):
        self.spec_path = Path(spec_path)
        if not self.spec_path.exists():
            raise FileNotFoundError(f"Spec file not found: {spec_path}")
        self.content = self.spec_path.read_text()
        self.lines = self.content.splitlines()

    def save(self, output_path: str | Path | None = None):
        """Save spec file."""
        output = Path(output_path) if output_path else self.spec_path
        output.write_text("\n".join(self.lines) + "\n")
        logger.info(f"Saved spec file: {output}")

    def get_name(self) -> str | None:
        """Get package name from spec file."""
        for line in self.lines:
            match = re.match(r"^Name:\s+(.+)$", line, re.IGNORECASE)
            if match:
                return re.sub(r"%\{[^}]+\}", "", match.group(1).strip()).strip()
        return None

    def get_version(self) -> str | None:
        """Get version from spec file."""
        for line in self.lines:
            match = re.match(r"^Version:\s+(.+)$", line, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def get_epoch(self) -> str | None:
        """Get epoch from spec file."""
        for line in self.lines:
            match = re.match(r"^Epoch:\s+(.+)$", line, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def get_release(self) -> str | None:
        """Get current release from spec file."""
        for line in self.lines:
            match = re.match(r"^Release:\s+(.+)$", line, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def get_nvr(self) -> tuple[str | None, str | None, str | None]:
        """Get Name-Version-Release."""
        return self.get_name(), self.get_version(), self.get_release()

    def get_full_nvr(self) -> str | None:
        """Get full NVR string including epoch if present."""
        name, version, release = self.get_nvr()
        if not all([name, version, release]):
            return None
        epoch = self.get_epoch()
        if epoch:
            return f"{epoch}:{name}-{version}-{release}"
        return f"{name}-{version}-{release}"

    def bump_release(self) -> tuple[str, str]:
        """
        Bump release version for golang rebuild.

        Patterns:
        - 3%{?dist}     -> 3%{?dist}.1
        - 3%{?dist}.1   -> 3%{?dist}.2
        - 5             -> 5.1

        Returns:
            Tuple of (old_release, new_release)
        """
        release_line_idx = None
        current_release = None

        for idx, line in enumerate(self.lines):
            match = re.match(r"^Release:\s+(.+)$", line, re.IGNORECASE)
            if match:
                release_line_idx = idx
                current_release = match.group(1).strip()
                break

        if release_line_idx is None or current_release is None:
            raise ValueError("Release: line not found in spec file")

        # Try with dist macro
        match = re.match(r"^(\d+)(%\{?\??dist}?)(?:\.(\d+))?$", current_release)
        if match:
            base_number = match.group(1)
            dist_macro = match.group(2)
            minor = match.group(3)
            if minor:
                new_release = f"{base_number}{dist_macro}.{int(minor) + 1}"
            else:
                new_release = f"{base_number}{dist_macro}.1"
        else:
            # Try without dist macro
            match = re.match(r"^(\d+)(?:\.(\d+))?$", current_release)
            if match:
                base_number = match.group(1)
                minor = match.group(2)
                new_release = f"{base_number}.{int(minor) + 1}" if minor else f"{base_number}.1"
            else:
                raise ValueError(f"Unsupported release format: {current_release}")

        self.lines[release_line_idx] = f"Release: {new_release}"
        logger.info(f"Bumped release: {current_release} -> {new_release}")
        return current_release, new_release

    def find_changelog_line(self) -> int:
        """Find %changelog section line number."""
        for idx, line in enumerate(self.lines):
            if re.match(r"^%changelog\s*$", line, re.IGNORECASE):
                return idx
        raise ValueError("%changelog section not found in spec file")

    def add_changelog_entry(
        self,
        golang_version: str,
        cves: list[str],
        jiras: list[str],
        author_name: str,
        author_email: str,
        custom_message: str | None = None,
    ):
        """
        Add changelog entry for golang rebuild.

        Args:
            custom_message: If provided, replaces the default "Rebuilding with new golang X.Y.Z" line.
        """
        nvr = self.get_full_nvr()
        if not nvr:
            raise ValueError("Cannot generate NVR for changelog entry")

        date_str = format_date_for_changelog()
        description = custom_message or f"Rebuilding with new golang {golang_version}"
        entry_lines = [
            f"* {date_str} {author_name} <{author_email}> - {nvr}",
            f"- {description}",
            f"- Fixes: {format_cve_list(cves)}",
            f"- Resolves: {format_jira_list(jiras)}",
        ]

        changelog_idx = self.find_changelog_line()
        insert_idx = changelog_idx + 1

        if insert_idx < len(self.lines) and self.lines[insert_idx].strip():
            entry_lines.append("")

        for i, entry_line in enumerate(entry_lines):
            self.lines.insert(insert_idx + i, entry_line)

        logger.info(f"Added changelog entry for golang {golang_version}")

    def get_latest_changelog_entry(self) -> str | None:
        """Get the latest changelog entry."""
        try:
            changelog_idx = self.find_changelog_line()
        except ValueError:
            return None

        entry_lines = []
        for idx in range(changelog_idx + 1, len(self.lines)):
            line = self.lines[idx]
            if line.startswith("%") and idx > changelog_idx + 1:
                break
            if line.startswith("*") and entry_lines:
                break
            entry_lines.append(line)

        return "\n".join(entry_lines).strip()

    def update_commit0(self, new_commit: str) -> str | None:
        """
        Update %global commit0 (or %global commit) in spec file.

        Searches for lines like:
            %global commit0 abc123...
            %global commit abc123...

        Args:
            new_commit: New commit hash

        Returns:
            Old commit hash, or None if not found
        """
        patterns = [
            (r"^(%global\s+commit0\s+)(\S+)$", "commit0"),
            (r"^(%global\s+commit\s+)(\S+)$", "commit"),
        ]

        for idx, line in enumerate(self.lines):
            for pattern, name in patterns:
                match = re.match(pattern, line, re.IGNORECASE)
                if match:
                    old_commit = match.group(2)
                    self.lines[idx] = f"{match.group(1)}{new_commit}"
                    logger.info(f"Updated %global {name}: {old_commit[:12]}... -> {new_commit[:12]}...")
                    return old_commit

        logger.warning("No %global commit0 or %global commit found in spec file")
        return None

    def validate_spec(self) -> list[str]:
        """Validate spec file format. Returns list of errors."""
        errors = []
        if not self.get_name():
            errors.append("Name: field not found")
        if not self.get_version():
            errors.append("Version: field not found")
        if not self.get_release():
            errors.append("Release: field not found")
        try:
            self.find_changelog_line()
        except ValueError:
            errors.append("%changelog section not found")
        return errors

    @staticmethod
    def find_spec_file(directory: Path) -> Path | None:
        """Find .spec file in directory."""
        spec_files = list(directory.glob("*.spec"))
        if not spec_files:
            return None
        if len(spec_files) > 1:
            raise ValueError(f"Multiple .spec files found in {directory}: {spec_files}")
        return spec_files[0]


def bump_spec_for_golang_rebuild(
    spec_path: str | Path,
    golang_version: str,
    cves: list[str],
    jiras: list[str],
    author_name: str,
    author_email: str,
    commit_hash: str | None = None,
    custom_message: str | None = None,
) -> tuple[str, str]:
    """
    Bump spec file for golang rebuild (convenience function).

    Args:
        commit_hash: If provided, updates %global commit0 in spec.
        custom_message: If provided, replaces default changelog description.

    Returns:
        Tuple of (old_release, new_release)
    """
    spec = SpecFile(spec_path)

    # Update commit hash if provided
    if commit_hash:
        spec.update_commit0(commit_hash)

    old_release, new_release = spec.bump_release()
    spec.add_changelog_entry(
        golang_version=golang_version,
        cves=cves,
        jiras=jiras,
        author_name=author_name,
        author_email=author_email,
        custom_message=custom_message,
    )
    spec.save()
    return old_release, new_release
