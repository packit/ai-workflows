"""CVE text parsing shared by agents and Jira tools without loading Jira clients."""

import re

CVE_ID_PATTERN = re.compile(r"(?<![A-Z0-9])(CVE-[0-9]{4}-[0-9]{4,})(?![A-Z0-9])")


def extract_cve_ids(summary: str | None) -> str | None:
    """Return every valid CVE in a summary as a normalized comma-separated set."""
    if not isinstance(summary, str):
        return None
    cve_ids = sorted(set(CVE_ID_PATTERN.findall(summary.upper())))
    return ",".join(cve_ids) if cve_ids else None
