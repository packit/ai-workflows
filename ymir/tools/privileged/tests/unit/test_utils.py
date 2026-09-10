import os
import shutil
from datetime import datetime, timedelta

import pytest
from flexmock import flexmock

from ymir.tools.privileged.utils import ACTIVE_WORKSPACE_MARKER, clean_stale_repositories


@pytest.fixture
def test_directories(mock_git_repo_basepath):
    """Fixture that creates test directories with different ages."""
    temp_dir = mock_git_repo_basepath

    # Create test directories
    old_rhel_dir = temp_dir / "rhel-old-package"
    old_other_dir = temp_dir / "consolidation-pkg-c9s-123456"
    new_dir = temp_dir / "rhel-new-package"

    old_rhel_dir.mkdir()
    old_other_dir.mkdir()
    new_dir.mkdir()

    # Set different timestamps
    old_time = datetime.now() - timedelta(days=15)
    os.utime(old_rhel_dir, (old_time.timestamp(), old_time.timestamp()))
    os.utime(old_other_dir, (old_time.timestamp(), old_time.timestamp()))

    new_time = datetime.now() - timedelta(days=1)
    os.utime(new_dir, (new_time.timestamp(), new_time.timestamp()))

    return {
        "temp_dir": temp_dir,
        "old_rhel_dir": old_rhel_dir,
        "old_other_dir": old_other_dir,
        "new_dir": new_dir,
    }


@pytest.mark.asyncio
async def test_clean_stale_repositories(test_directories):
    """Test the clean_stale_repositories function."""
    old_rhel_dir = test_directories["old_rhel_dir"]
    old_other_dir = test_directories["old_other_dir"]
    new_dir = test_directories["new_dir"]

    result = await clean_stale_repositories()

    assert result == 2

    assert not old_rhel_dir.is_dir()
    assert not old_other_dir.is_dir()
    assert new_dir.is_dir()


@pytest.mark.asyncio
async def test_clean_stale_repositories_no_stale_directories(mock_git_repo_basepath):
    """Test clean_stale_repositories when no stale directories exist."""
    temp_dir = mock_git_repo_basepath

    recent_dir = temp_dir / "rhel-recent-package"
    recent_dir.mkdir()

    result = await clean_stale_repositories()

    assert result == 0

    assert recent_dir.is_dir()


@pytest.mark.asyncio
async def test_clean_stale_repositories_cleans_container_children(mock_git_repo_basepath):
    """Test that children inside applicability/ and merge_requests/ are cleaned."""
    temp_dir = mock_git_repo_basepath
    old_time = datetime.now() - timedelta(days=15)

    applicability = temp_dir / "applicability"
    applicability.mkdir()
    old_child = applicability / "RHEL-12345"
    old_child.mkdir()
    os.utime(old_child, (old_time.timestamp(), old_time.timestamp()))
    new_child = applicability / "RHEL-99999"
    new_child.mkdir()

    mr_dir = temp_dir / "merge_requests"
    mr_dir.mkdir()
    old_mr = mr_dir / "_redhat_rhel_rpms_bash_123"
    old_mr.mkdir()
    os.utime(old_mr, (old_time.timestamp(), old_time.timestamp()))

    result = await clean_stale_repositories()

    assert result == 2
    assert not old_child.is_dir()
    assert new_child.is_dir()
    assert applicability.is_dir()
    assert not old_mr.is_dir()
    assert mr_dir.is_dir()


@pytest.mark.asyncio
async def test_clean_stale_repositories_cleans_execution_workspaces(mock_git_repo_basepath):
    old_workspace = mock_git_repo_basepath / "Backport" / "RHEL-12345" / "old"
    current_workspace = mock_git_repo_basepath / "Backport" / "RHEL-12345" / "current"
    old_workspace.mkdir(parents=True)
    current_workspace.mkdir()
    old_time = datetime.now() - timedelta(days=15)
    os.utime(old_workspace, (old_time.timestamp(), old_time.timestamp()))

    result = await clean_stale_repositories()

    assert result == 1
    assert not old_workspace.exists()
    assert current_workspace.is_dir()
    assert current_workspace.parent.is_dir()


@pytest.mark.asyncio
async def test_clean_stale_repositories_removes_empty_execution_issue_directory(mock_git_repo_basepath):
    workspace = mock_git_repo_basepath / "Backport" / "RHEL-12345" / "old"
    workspace.mkdir(parents=True)
    old_time = datetime.now() - timedelta(days=15)
    os.utime(workspace, (old_time.timestamp(), old_time.timestamp()))

    result = await clean_stale_repositories()

    assert result == 1
    assert not workspace.parent.exists()


@pytest.mark.asyncio
async def test_clean_stale_repositories_preserves_active_workspace(mock_git_repo_basepath):
    workspace = mock_git_repo_basepath / "Backport" / "RHEL-12345" / "active"
    workspace.mkdir(parents=True)
    old_time = datetime.now() - timedelta(days=15)
    os.utime(workspace, (old_time.timestamp(), old_time.timestamp()))
    (workspace / ACTIVE_WORKSPACE_MARKER).touch()

    result = await clean_stale_repositories()

    assert result == 0
    assert workspace.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path_parts",
    [("Backport",), ("Backport", "RHEL-12345"), ("Backport", "RHEL-12345", "old")],
)
async def test_clean_stale_repositories_does_not_follow_workspace_symlinks(
    mock_git_repo_basepath, tmp_path, path_parts
):
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir()
    (external / "keep").write_text("keep")
    link_path = mock_git_repo_basepath.joinpath(*path_parts)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(external, target_is_directory=True)
    old_time = datetime.now() - timedelta(days=15)
    os.utime(external, (old_time.timestamp(), old_time.timestamp()))

    result = await clean_stale_repositories()

    assert result == 0
    assert (external / "keep").read_text() == "keep"


@pytest.mark.asyncio
async def test_clean_stale_repositories_error_handling(test_directories):
    """Test clean_stale_repositories error handling."""
    flexmock(shutil).should_receive("rmtree").and_raise(OSError("Permission denied"))

    result = await clean_stale_repositories()

    assert result == 0
