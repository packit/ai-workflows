import os
import pytest
import shutil

from pathlib import Path
from datetime import datetime, timedelta
from flexmock import flexmock

from utils import clean_stale_repositories


@pytest.fixture
def mock_git_repo_basepath(tmp_path, monkeypatch):
    """Fixture to mock GIT_REPO_BASEPATH environment variable."""
    monkeypatch.setenv('GIT_REPO_BASEPATH', str(tmp_path))
    return tmp_path


@pytest.fixture
def test_directories(mock_git_repo_basepath):
    """Fixture that creates test directories with different ages."""
    temp_dir = mock_git_repo_basepath

    # Create test directories
    old_dir = temp_dir / "rhel-old-package"
    new_dir = temp_dir / "rhel-new-package"
    other_dir = temp_dir / "other-package"

    old_dir.mkdir()
    new_dir.mkdir()
    other_dir.mkdir()

    # Set different timestamps
    old_time = datetime.now() - timedelta(days=15)
    os.utime(old_dir, (old_time.timestamp(), old_time.timestamp()))

    new_time = datetime.now() - timedelta(days=5)
    os.utime(new_dir, (new_time.timestamp(), new_time.timestamp()))

    return {
        'temp_dir': temp_dir,
        'old_dir': old_dir,
        'new_dir': new_dir,
        'other_dir': other_dir
    }

@pytest.mark.asyncio
async def test_clean_stale_repositories(test_directories):
    """Test the clean_stale_repositories function."""
    old_dir = test_directories['old_dir']
    new_dir = test_directories['new_dir']
    other_dir = test_directories['other_dir']

    result = await clean_stale_repositories()

    assert result == 1

    assert not old_dir.is_dir()
    assert new_dir.is_dir()
    assert other_dir.is_dir()


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
async def test_clean_stale_repositories_error_handling(test_directories):
    """Test clean_stale_repositories error handling."""
    old_dir = test_directories['old_dir']

    flexmock(shutil).should_receive('rmtree').with_args(Path(old_dir)).and_raise(OSError("Permission denied"))

    result = await clean_stale_repositories()

    assert result == 0
