import pytest
from flexmock import flexmock
from specfile.utils import NEVR

from ymir.common.version_utils import (
    detect_modular_issue,
    extract_downstream_package,
    get_maintenance_rhel_branch,
    is_modular,
    is_older_zstream,
    nevr_matches_fix_version_stream,
    parse_branch_name,
    parse_module_stream,
    parse_rhel_version,
    parse_zstream_branch_name,
)


@pytest.mark.parametrize(
    "version, expected",
    [
        # Y-stream versions
        ("rhel-9.8", ("9", "8", False)),
        ("rhel-10.2", ("10", "2", False)),
        ("rhel-8.10", ("8", "10", False)),
        # Z-stream versions (standard format)
        ("rhel-9.7.z", ("9", "7", True)),
        ("rhel-8.10.z", ("8", "10", True)),
        ("rhel-10.1.z", ("10", "1", True)),
        # Z-stream versions with .0 suffix
        ("rhel-9.0.0.z", ("9", "0", True)),
        ("rhel-8.8.0.z", ("8", "8", True)),
        ("rhel-9.6.0.z", ("9", "6", True)),
        # Case insensitive
        ("RHEL-9.7.z", ("9", "7", True)),
        ("Rhel-8.10.z", ("8", "10", True)),
        # Invalid formats
        ("invalid", None),
        ("rhel-9", None),
        ("c9s", None),
        ("rhel-", None),
        ("", None),
        ("rhel-9.8.1.z", None),  # .1 suffix (not .0) should not match
    ],
)
def test_parse_rhel_version(version, expected):
    result = parse_rhel_version(version)
    assert result == expected


@pytest.mark.parametrize(
    "nvr, fix_version, expected",
    [
        # Z-stream ticket: only Z-stream builds (.elX_Y) match
        ("go-fdo-client-1.0.0-4.el10_2.7", "rhel-10.2.z", True),
        ("go-fdo-client-1.0.0-3.el10_2", "rhel-10.2.z", True),
        ("foo-1.5-3.el9_8", "rhel-9.8.z", True),
        # Z-stream ticket must reject a higher-EVR Y-stream / CentOS Stream build
        ("foo-2.0-1.el9", "rhel-9.8.z", False),
        # Wrong minor / major are rejected
        ("foo-1.5-3.el9_7", "rhel-9.8.z", False),
        ("foo-1.5-3.el8_8", "rhel-9.8.z", False),
        # No false substring match (minor 20 must not match minor 2)
        ("foo-1.5-3.el10_20", "rhel-10.2.z", False),
        # Y-stream ticket: both CentOS Stream (.el9) and RHEL branch (.el9_8)
        # builds legitimately back it — the former carries fixes developed in
        # Stream, the latter fixes that shipped as part of the GA itself.
        ("foo-2.0-1.el9", "rhel-9.8", True),
        ("foo-1.5-3.el9_8", "rhel-9.8", True),
        # A different major is still rejected for a Y-stream ticket
        ("foo-1.5-3.el8_8", "rhel-9.8", False),
        # Undeterminable inputs are kept (no dist tag or unparseable fix version)
        ("foo-1.5-3", "rhel-9.8.z", True),
        ("foo-1.5-3.el9_8", "garbage", True),
    ],
)
def test_nevr_matches_fix_version_stream(nvr, fix_version, expected):
    assert nevr_matches_fix_version_stream(NEVR.from_string(nvr), fix_version) is expected


@pytest.mark.parametrize(
    "branch, expected",
    [
        # RHEL 8/9 branch format (with .0 suffix)
        ("rhel-9.7.0", ("9", "7")),
        ("rhel-8.10.0", ("8", "10")),
        ("rhel-9.0.0", ("9", "0")),
        # RHEL 10+ branch format (without .0 suffix)
        ("rhel-10.1", ("10", "1")),
        ("rhel-10.0", ("10", "0")),
        # Case insensitive
        ("RHEL-9.7.0", ("9", "7")),
        # Not Z-stream branch names
        ("c9s", None),
        ("c10s", None),
        ("rhel-10-main", None),
        ("rhel-9.7.z", None),  # version string, not branch
        ("invalid", None),
        ("", None),
    ],
)
def test_parse_zstream_branch_name(branch, expected):
    result = parse_zstream_branch_name(branch)
    assert result == expected


@pytest.mark.parametrize(
    "branch, expected",
    [
        # RHEL 8/9 branch format (with .0 suffix)
        ("rhel-9.7.0", ("9", "7")),
        ("rhel-8.10.0", ("8", "10")),
        ("rhel-9.0.0", ("9", "0")),
        # RHEL 10+ branch format (without .0 suffix)
        ("rhel-10.1", ("10", "1")),
        ("rhel-10.0", ("10", "0")),
        # Case insensitive
        ("RHEL-9.7.0", ("9", "7")),
        # CentOS Stream branches
        ("c9s", ("9", None)),
        ("c10s", ("10", None)),
        # RHEL main branches
        ("rhel-10-main", ("10", None)),
        # Modular stream branches
        ("stream-nginx-1.26-rhel-9.9.0", ("9", "9")),
        ("stream-squid-4-rhel-8.10.0", ("8", "10")),
        ("stream-postgresql-12-rhel-9.8.0", ("9", "8")),
        ("stream-nodejs-18-rhel-10.2.0", ("10", "2")),
        ("STREAM-nginx-1.26-rhel-9.9.0", ("9", "9")),
        # Not branch names
        ("rhel-9.7.z", None),  # version string, not branch
        ("invalid", None),
        ("", None),
        ("stream-nginx-1.26", None),  # missing rhel version suffix
    ],
)
def test_parse_branch_name(branch, expected):
    result = parse_branch_name(branch)
    assert result == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version_or_branch, expected",
    [
        # Older z-stream version strings
        ("rhel-9.6.z", True),
        ("rhel-9.4.z", True),
        ("rhel-8.8.z", True),
        ("rhel-10.0.z", True),
        # Current z-stream (not older)
        ("rhel-9.7.z", False),
        ("rhel-8.10.z", False),
        ("rhel-10.1.z", False),
        # Newer than current (not older)
        ("rhel-9.8.z", False),
        # Y-stream version strings (not z-stream)
        ("rhel-9.8", False),
        ("rhel-10.2", False),
        # Older z-stream via branch names
        ("rhel-9.6.0", True),
        ("rhel-9.4.0", True),
        ("rhel-10.0", True),
        # Current or newer via branch names
        ("rhel-9.7.0", False),
        ("rhel-10.1", False),
        # CentOS Stream branches
        ("c9s", False),
        ("c10s", False),
        # Invalid
        ("invalid", False),
        ("", False),
    ],
)
async def test_is_older_zstream(version_or_branch, expected):
    CURRENT_Z_STREAMS = {
        "8": "rhel-8.10.z",
        "9": "rhel-9.7.z",
        "10": "rhel-10.1.z",
    }
    result = await is_older_zstream(version_or_branch, CURRENT_Z_STREAMS)
    assert result == expected


RHEL_CONFIG = {
    "current_y_streams": {"9": "rhel-9.8", "10": "rhel-10.2"},
    "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.7.z", "10": "rhel-10.1.z"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch, expected",
    [
        ("c8s", "rhel-8.10.0"),
        ("c9s", None),
        ("c10s", None),
        ("rhel-8.10.0", None),
        ("rhel-8-main", None),
        ("rhel-9.7.0", None),
        ("rhel-10-main", None),
        ("invalid", None),
        ("", None),
    ],
)
async def test_get_maintenance_rhel_branch(branch, expected):
    from ymir.common import config

    async def mock_load_rhel_config():
        return RHEL_CONFIG

    flexmock(config).should_receive("load_rhel_config").replace_with(mock_load_rhel_config)
    result = await get_maintenance_rhel_branch(branch)
    assert result == expected


@pytest.mark.parametrize(
    "summary, component, expected",
    [
        ("CVE-2025-12345 postgresql:16/postgresql: overflow", "postgresql", ("postgresql", "16")),
        ("CVE-2025-12345 postgresql:18/postgresql: overflow", "postgresql", ("postgresql", "18")),
        ("postgresql:12/postgresql: overflow", "postgresql", ("postgresql", "12")),
        ("CVE-2025-12345 buffer overflow in curl", "curl", None),
        (
            "CVE-2025-12345 CVE-2025-67890 postgresql:16/postgresql: overflow",
            "postgresql",
            ("postgresql", "16"),
        ),
        ("CVE-2025-12345 postgresql:16/postgis: overflow", "postgis", ("postgresql", "16")),
        (None, "postgresql", None),
        ("CVE-2025-12345 postgresql:16/postgresql: overflow", None, None),
        ("", "postgresql", None),
    ],
)
def test_parse_module_stream(summary, component, expected):
    assert parse_module_stream(summary, component) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("postgresql:16/postgis", "postgis"),
        ("nodejs:18/nodejs", "nodejs"),
        ("perl:5.32/perl-IO-Socket-SSL", "perl-IO-Socket-SSL"),
        ("libtiff", "libtiff"),
        ("regular-component", "regular-component"),
        (None, None),
        ("", None),
    ],
)
def test_extract_downstream_package(raw, expected):
    assert extract_downstream_package(raw) == expected


def test_is_modular_requires_package_not_full_modular_string():
    summary = "postgresql:16/postgis: PostGIS: vuln"
    raw = "postgresql:16/postgis"
    assert is_modular(summary, raw) is False
    assert is_modular(summary, extract_downstream_package(raw)) is True


def test_parse_module_stream_requires_package_not_full_modular_string():
    summary = "postgresql:16/postgis: PostGIS: vuln"
    raw = "postgresql:16/postgis"
    assert parse_module_stream(summary, raw) is None
    assert parse_module_stream(summary, extract_downstream_package(raw)) == ("postgresql", "16")


# --- detect_modular_issue -------------------------------------------------


class TestDetectModularIssue:
    """Tests for detect_modular_issue — the shared helper used by agents."""

    def test_raw_component_structural_match(self):
        """module:stream/package format is detected without needing summary."""
        assert (
            detect_modular_issue(
                jira_summary=None,
                raw_downstream_component="postgresql:16/postgis",
            )
            is True
        )

    def test_raw_component_non_modular(self):
        """Plain package name is not modular without matching summary."""
        assert (
            detect_modular_issue(
                jira_summary="CVE-2026-1234 postgresql16: some vuln [rhel-10.2.z]",
                raw_downstream_component="postgresql16",
            )
            is False
        )

    def test_summary_fallback_after_extraction(self):
        """When raw is module:stream/package, summary check uses extracted package."""
        assert (
            detect_modular_issue(
                jira_summary="CVE-2026-1234 postgresql:16/postgis: PostGIS vuln [rhel-9.8.z]",
                raw_downstream_component="postgresql:16/postgis",
            )
            is True
        )

    def test_extracted_component_override(self):
        """Explicit downstream_component overrides extraction from raw."""
        assert (
            detect_modular_issue(
                jira_summary="CVE-2026-1234 postgresql:16/postgis: PostGIS vuln [rhel-9.8.z]",
                raw_downstream_component=None,
                downstream_component="postgis",
            )
            is True
        )

    def test_all_none(self):
        """All None inputs → not modular."""
        assert detect_modular_issue(None, None, None) is False

    def test_summary_only_modular(self):
        """Summary alone detects modular when component is passed."""
        assert (
            detect_modular_issue(
                jira_summary="CVE-2026-1234 squid:4/squid: Denial of Service [rhel-8.10.z]",
                raw_downstream_component=None,
                downstream_component="squid",
            )
            is True
        )

    def test_slash_in_path_not_modular(self):
        """A slash that isn't module:stream/package is not modular."""
        assert (
            detect_modular_issue(
                jira_summary="some issue",
                raw_downstream_component="path/to/package",
            )
            is False
        )

    def test_post_extract_triage_state(self):
        """Simulates new triage state: raw has module:stream/package, dc is extracted."""
        assert (
            detect_modular_issue(
                jira_summary="CVE-2026-73515 postgresql:16/postgis: Memory vuln [rhel-9.8.z]",
                raw_downstream_component="postgresql:16/postgis",
                downstream_component="postgis",
            )
            is True
        )

    def test_pre_extract_triage_state(self):
        """Simulates old triage state: dc is the raw module:stream/package string."""
        assert (
            detect_modular_issue(
                jira_summary="CVE-2026-73515 postgresql:16/postgis: Memory vuln [rhel-9.8.z]",
                raw_downstream_component=None,
                downstream_component="postgresql:16/postgis",
            )
            is True
        )


def test_version_utils_importable_without_specfile():
    """ymir/common must be importable without specfile (API image dependency set).

    The API container image ships ymir/common with a minimal Python package set
    that does not include specfile; importing ymir.common loads version_utils,
    so a module-level specfile import breaks the API server at startup.
    """
    import subprocess
    import sys

    code = "\n".join(
        [
            "import sys",
            "",
            "class _Blocker:",
            "    def find_spec(self, name, path=None, target=None):",
            "        if name == 'specfile' or name.startswith('specfile.'):",
            "            raise ImportError(name)",
            "",
            "sys.meta_path.insert(0, _Blocker())",
            "import ymir.common  # noqa: F401",
        ]
    )
    subprocess.run([sys.executable, "-c", code], check=True)
