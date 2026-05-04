import pytest

from ymir.common.version_utils import (
    is_older_zstream,
    parse_branch_name,
    parse_rhel_version,
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
        # Not branch names
        ("c9s", None),
        ("c10s", None),
        ("rhel-9.7.z", None),  # version string, not branch
        ("invalid", None),
        ("", None),
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
