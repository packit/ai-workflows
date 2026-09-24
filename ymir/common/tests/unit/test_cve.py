import pytest

from ymir.common.cve import extract_cve_ids


def test_extract_cve_ids_returns_normalized_deduplicated_set():
    assert extract_cve_ids("CVE-2026-1234, cve-2025-5678, CVE-2026-1234") == "CVE-2025-5678,CVE-2026-1234"


@pytest.mark.parametrize("summary", [None, "", "No CVE here", 123])
def test_extract_cve_ids_accepts_missing_or_invalid_summary(summary):
    assert extract_cve_ids(summary) is None


@pytest.mark.parametrize("summary", ["CVE-٢٠٢٦-١٢٣٤", "notCVE-2026-1234", "CVE-2026-1234suffix"])
def test_extract_cve_ids_rejects_invalid_identifiers(summary):
    assert extract_cve_ids(summary) is None
