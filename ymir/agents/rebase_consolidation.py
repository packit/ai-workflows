import re
from pathlib import Path

from specfile import Specfile

import ymir.agents.tasks as tasks


def uses_autochangelog(spec_path: Path) -> bool:
    """Return whether the spec delegates changelog generation to rpmautospec."""
    with Specfile(spec_path) as spec:
        return spec.has_autochangelog


def changelog_entry_headers(spec_path: Path) -> list[str]:
    """Return RPM changelog headers in newest-to-oldest order."""
    with Specfile(spec_path) as spec, spec.changelog() as changelog:
        return [entry.header for entry in reversed(changelog)] if changelog else []


def has_new_latest_changelog_entry(spec_path: Path, previous_headers: list[str]) -> bool:
    """Return whether a new entry was prepended without replacing historical entries."""
    headers = changelog_entry_headers(spec_path)
    return len(headers) > len(previous_headers) and headers[1:] == previous_headers


def add_jira_tickets_to_latest_changelog_entry(
    spec_path: Path,
    jira_issues: list[str],
    previous_headers: list[str] | None = None,
) -> bool:
    """Add missing Jira Resolves lines to the newest spec changelog entry.

    Returns ``True`` only when the spec file was changed.  A spec with an empty
    ``%changelog`` section has no entry to update, so it is deliberately left
    untouched.
    """
    if previous_headers is not None and not has_new_latest_changelog_entry(spec_path, previous_headers):
        raise ValueError("A new changelog entry is required before adding consolidation metadata")

    with Specfile(spec_path) as spec, spec.changelog() as changelog:
        if not changelog:
            return False

        entry = changelog[-1]
        references = [
            reference for line in entry.content if (reference := tasks.parse_changelog_reference(line))
        ]
        resolves_references = [
            reference for reference in references if reference.tag.casefold() == "resolves"
        ]
        suffix_lines = [
            (index, suffix)
            for index, line in enumerate(entry.content)
            if not tasks.is_changelog_reference_line(line) and (suffix := tasks._extract_jira_suffix(line))
        ]
        existing_issues = {
            issue.upper()
            for reference in resolves_references
            for issue in re.findall(r"\bRHEL-\d+\b", reference.line, re.IGNORECASE)
        }
        existing_issues.update(
            issue.upper()
            for _, suffix in suffix_lines
            for issue in re.findall(r"\bRHEL-\d+\b", suffix, re.IGNORECASE)
        )
        missing_issues = [
            issue for issue in dict.fromkeys(jira_issues) if issue.upper() not in existing_issues
        ]
        if not missing_issues:
            return False

        inline_references = [reference for reference in resolves_references if reference.is_inline]
        if inline_references:
            reference = inline_references[0]
            index = next(
                index
                for index, line in enumerate(entry.content)
                if tasks.parse_changelog_reference(line) == reference
            )
            entry.content[index] = reference.render(issues=[*reference.issues, *missing_issues])
            return True

        if suffix_lines:
            index, suffix = suffix_lines[0]
            line = entry.content[index]
            suffix_issues = re.findall(r"\bRHEL-\d+\b", suffix, re.IGNORECASE)
            suffix_issues.extend(issue for issue in missing_issues if issue.upper() not in existing_issues)
            suffix_start = line.rfind(suffix)
            entry.content[index] = (
                line[:suffix_start]
                + tasks._rewrite_jira_suffix(suffix, suffix_issues)
                + line[suffix_start + len(suffix) :]
            )
            return True

        reference_prefix = resolves_references[0].prefix if resolves_references else "- "
        reference_tag = resolves_references[0].tag if resolves_references else "Resolves"
        entry.content.extend(f"{reference_prefix}{reference_tag}: {issue}" for issue in missing_issues)
        return True
