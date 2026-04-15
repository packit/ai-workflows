import pytest


@pytest.fixture
def mock_git_repo_basepath(tmp_path, monkeypatch):
    """Fixture to mock GIT_REPO_BASEPATH environment variable."""
    monkeypatch.setenv('GIT_REPO_BASEPATH', str(tmp_path))
    return tmp_path
