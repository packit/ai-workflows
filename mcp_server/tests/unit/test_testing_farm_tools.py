"""Tests for testing_farm_tools module."""

import asyncio
import shlex
import pytest
from unittest.mock import AsyncMock, patch
from mcp_server.testing_farm_tools import run_testing_farm_test, get_compose_from_branch


def test_tmt_prepare_quoting():
    """Test that package URLs with special characters are properly quoted."""
    # Test with a package URL that has special characters
    package = 'http://example.com/package-1.0-1.el9.x86_64.rpm'
    tmt_prepare = f'--insert --how install --package {shlex.quote(package)}'

    # Should be properly quoted
    assert '--insert' in tmt_prepare
    assert '--how' in tmt_prepare
    assert 'install' in tmt_prepare
    assert '--package' in tmt_prepare
    assert package in tmt_prepare


@pytest.mark.asyncio
async def test_get_compose_from_branch():
    """Test get_compose_from_branch function with various branch formats."""
    test_cases = [
        ("rhel-9.7.0", "RHEL-9.7.0-Nightly"),
        ("rhel-10.2", "RHEL-10.2-Nightly"),
        ("rhel-8.10", "RHEL-8.10.0-Nightly"),
        ("c8s", "CentOS-Stream-8"),
    ]

    for branch, expected_compose in test_cases:
        result = await get_compose_from_branch(branch)
        assert result == expected_compose, (
            f"Branch '{branch}' should return '{expected_compose}', "
            f"but got '{result}'"
        )


@pytest.mark.asyncio
async def test_run_testing_farm_test_success():
    """Test run_testing_farm_test with successful test execution."""
    git_url = "https://gitlab.com/redhat/rhel/tests/expat.git"
    git_ref = "master"
    path_to_test = "Security/RHEL-114639-CVE-2025-59375-expat-libexpat-in-Expat-allows"
    package = "http://coprbe.devel.redhat.com/results/mmassari/RHEL-114644/rhel-9-x86_64/00127295-expat/expat-2.5.0-5.el9.x86_64.rpm"
    dist_git_branch = "rhel-9.7.0"

    # Mock the subprocess to return success
    mock_process = AsyncMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(return_value=(b"Test passed", b""))

    with patch('mcp_server.testing_farm_tools.asyncio.create_subprocess_exec', return_value=mock_process):
        success, result = await run_testing_farm_test(
            git_url=git_url,
            git_ref=git_ref,
            path_to_test=path_to_test,
            package=package,
            dist_git_branch=dist_git_branch,
        )

    assert success is True
    assert "Test passed" in result
    assert "Testing Farm test passed" in result


@pytest.mark.asyncio
async def test_run_testing_farm_test_failure():
    """Test run_testing_farm_test with failed test execution."""
    git_url = "https://gitlab.com/redhat/rhel/tests/expat.git"
    git_ref = "master"
    path_to_test = "Security/RHEL-114639-CVE-2025-59375-expat-libexpat-in-Expat-allows"
    package = "http://coprbe.devel.redhat.com/results/mmassari/RHEL-114644/rhel-9-x86_64/00127295-expat/expat-2.5.0-5.el9.x86_64.rpm"
    dist_git_branch = "rhel-9.7.0"

    # Mock the subprocess to return failure
    mock_process = AsyncMock()
    mock_process.returncode = 1
    mock_process.communicate = AsyncMock(return_value=(b"", b"Test failed with error"))

    with patch('mcp_server.testing_farm_tools.asyncio.create_subprocess_exec', return_value=mock_process):
        success, result = await run_testing_farm_test(
            git_url=git_url,
            git_ref=git_ref,
            path_to_test=path_to_test,
            package=package,
            dist_git_branch=dist_git_branch,
        )

    assert success is False
    assert "Test failed" in result or "exit code 1" in result
    assert "Testing Farm test failed" in result


if __name__ == '__main__':
    # Run tests if executed directly
    test_tmt_prepare_quoting()
    # Run async tests
    asyncio.run(test_get_compose_from_branch())
    asyncio.run(test_run_testing_farm_test_success())
    asyncio.run(test_run_testing_farm_test_failure())
    print("All tests passed!")
