from unittest.mock import AsyncMock, patch

import pytest

from ymir.agents.tasks import fork_and_prepare_dist_git


@pytest.fixture
def git_repo_basepath(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_fork_and_prepare_dist_git_wipes_stale_working_dir(git_repo_basepath):
    """Re-running for the same JIRA issue must remove the previous working directory."""
    jira_issue = "RHEL-12345"
    package = "some-package"
    branch = "rhel-10.0"

    working_dir = git_repo_basepath / jira_issue
    working_dir.mkdir()
    stale_file = working_dir / "leftover-artifact.txt"
    stale_file.write_text("stale")

    mock_tools = [AsyncMock()]

    with (
        patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run_tool,
        patch("ymir.agents.tasks.check_subprocess", new_callable=AsyncMock),
    ):
        mock_run_tool.return_value = "https://fork.example.com"

        await fork_and_prepare_dist_git(
            jira_issue=jira_issue,
            package=package,
            dist_git_branch=branch,
            available_tools=mock_tools,
        )

    assert working_dir.is_dir(), "working_dir should be recreated"
    assert not stale_file.exists(), "stale artifacts from previous run should be gone"
