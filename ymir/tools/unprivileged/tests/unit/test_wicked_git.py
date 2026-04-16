import subprocess

import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError

from ymir.tools.unprivileged.wicked_git import (
    GitLogSearchTool,
    GitLogSearchToolInput,
    GitPatchApplyFinishTool,
    GitPatchApplyFinishToolInput,
    GitPatchApplyTool,
    GitPatchApplyToolInput,
    GitPatchCreationTool,
    GitPatchCreationToolInput,
    discover_patch_p,
    find_rej_files,
)


@pytest.fixture
def git_repo(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=repo_path, check=True)
    # Create a file and commit it
    file_path = repo_path / "file.txt"
    file_path.write_text("Line 1\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit\n\nCVE-2025-12345"],
        cwd=repo_path,
        check=True,
    )
    file_path.write_text("Line 1\nLine 2\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit2\n\nResolves: RHEL-123456"],
        cwd=repo_path,
        check=True,
    )
    subprocess.run(["git", "branch", "line-2"], cwd=repo_path, check=True)
    return repo_path


@pytest.mark.asyncio
async def test_git_patch_creation_tool_nonexistent_repo(tmp_path):
    # This test checks the error message for a non-existent repo path
    repo_path = tmp_path / "not_a_repo"
    patch_file_path = tmp_path / "patch.patch"
    tool = GitPatchCreationTool()
    with pytest.raises(ToolError) as e:
        await tool.run(
            input=GitPatchCreationToolInput(
                repository_path=str(repo_path),
                patch_file_path=str(patch_file_path),
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = e.value.message
    assert "Repository path does not exist" in result


@pytest.mark.asyncio
async def test_git_patch_creation_tool_success(git_repo, tmp_path):
    # Simulate a git-am session with a conflict by creating a new commit and then using format-patch
    # Create a new file and stage it
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=git_repo, check=True)
    new_file = git_repo / "file.txt"
    new_file.write_text("Line 1\nLine 3\n")
    subprocess.run(["git", "add", "file.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "Add line 3"], cwd=git_repo, check=True)

    patch_file = tmp_path / "patch.patch"
    subprocess.run(
        ["git", "format-patch", "-1", "HEAD", "--stdout"],
        cwd=git_repo,
        check=True,
        stdout=patch_file.open("w"),
    )

    subprocess.run(["git", "switch", "line-2"], cwd=git_repo, check=True)
    base_head_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Now apply the patch with git am
    # This will fail with a merge conflict, but we don't care about that
    apply_tool = GitPatchApplyTool()
    output = await apply_tool.run(
        input=GitPatchApplyToolInput(
            repository_path=str(git_repo),
            patch_file_path=str(patch_file),
        ),
    )
    assert "Patch application failed" in str(output.result)

    # resolve the conflict:
    new_file.write_text("Line 1\nLine 2\nLine 3\n")
    # remove rej file
    (git_repo / "file.txt.rej").unlink()

    # finish the patch application
    finish_tool = GitPatchApplyFinishTool()
    output = await finish_tool.run(
        input=GitPatchApplyFinishToolInput(
            repository_path=str(git_repo),
            patch_file_path=str(patch_file),
        ),
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    # Now use the tool to create a patch file from the repo
    tool = GitPatchCreationTool(options={"this_cannot_be_empty": "sure-why-not"})
    tool.options["base_head_commit"] = base_head_commit
    output_patch = tmp_path / "output.patch"
    output = await tool.run(
        input=GitPatchCreationToolInput(
            repository_path=str(git_repo),
            patch_file_path=str(output_patch),
        ),
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert "Successfully created a patch file" in result
    assert output_patch.exists()
    # The patch file should contain the commit message "Add line 3"
    assert "Add line 3" in output_patch.read_text()


@pytest.mark.asyncio
async def test_git_patch_creation_tool_with_hideous_patch_file(git_repo, tmp_path):
    """Verifies that GitPatchCreationTool can recover from a `git am` failure
    caused by a patch file without a proper header (i.e., missing author identity).
    """
    base_head_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    patch_file = tmp_path / "hideous-patch.patch"
    patch_file.write_text(
        "\nRotten plums and apples\n\n"
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -1,2 +1,3 @@\n"
        " Line 1\n"
        " Line 2\n"
        "+Line 3\n"
        "--\n"
        "2.51.0\n"
    )
    # Now apply the patch
    apply_tool = GitPatchApplyTool()
    output = await apply_tool.run(
        input=GitPatchApplyToolInput(
            repository_path=str(git_repo),
            patch_file_path=str(patch_file),
        ),
    )
    # verify the git-am fails with the expected error message
    assert "fatal: empty ident name (for <>) not allowed" in str(output.result)

    # finish the patch application
    finish_tool = GitPatchApplyFinishTool()
    output = await finish_tool.run(
        input=GitPatchApplyFinishToolInput(
            repository_path=str(git_repo),
            patch_file_path=str(patch_file),
        ),
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    # Now use the tool to create a patch file from the repo
    tool = GitPatchCreationTool(options={"this_cannot_be_empty": "sure-why-not"})
    tool.options["base_head_commit"] = base_head_commit
    output_patch = tmp_path / "output.patch"
    output = await tool.run(
        input=GitPatchCreationToolInput(
            repository_path=str(git_repo),
            patch_file_path=str(output_patch),
        ),
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert "Successfully created a patch file" in result
    assert output_patch.exists()
    # The patch file should contain the addition of 'Line 3'
    assert "+Line 3\n" in output_patch.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cve_id, jira_issue, expected",
    [
        ("CVE-2025-12345", "", "Found 1 matching commit(s) for 'CVE-2025-12345'"),
        ("CVE-2025-12346", "", "No matches found for 'CVE-2025-12346'"),
        ("", "RHEL-123456", "Found 1 matching commit(s) for 'RHEL-123456'"),
        ("", "RHEL-123457", "No matches found for 'RHEL-123457'"),
    ],
)
async def test_git_log_search_tool_found(git_repo, cve_id, jira_issue, expected):
    tool = GitLogSearchTool()
    output = await tool.run(
        input=GitLogSearchToolInput(
            repository_path=str(git_repo),
            cve_id=cve_id,
            jira_issue=jira_issue,
        )
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert expected in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch_content, expected_n",
    [
        (
            "diff --git a/file.txt b/file.txt\n"
            "index cb752151e..ceb5c5dca 100644\n"
            "--- a/file.txt\n"
            "+++ b/file.txt\n"
            "@@ -1,2 +1,3 @@\n"
            " Line 1\n"
            " Line 2\n"
            "+Line 3\n",
            1,
        ),
        (
            "diff --git a/z/file.txt b/z/file.txt\n"
            "index cb752151e..ceb5c5dca 100644\n"
            "--- a/z/file.txt\n"
            "+++ b/z/file.txt\n"
            "@@ -1,2 +1,3 @@\n"
            " Line 1\n"
            " Line 2\n"
            "+Line 3\n",
            2,
        ),
    ],
)
async def test_discover_patch_p(git_repo, tmp_path, patch_content, expected_n):
    patch_file = tmp_path / f"{expected_n}.patch"
    patch_file.write_text(patch_content)
    n = await discover_patch_p(patch_file, git_repo)
    assert n == expected_n


@pytest.mark.asyncio
async def test_find_rej_files(git_repo):
    (git_repo / ".gitignore").write_text("*.rej\n")
    (git_repo / "file.txt.rej").write_text("rej content")
    (git_repo / "foo-bar.rej").write_text("rej content 2")
    result = await find_rej_files(git_repo)
    assert sorted(result) == sorted(["file.txt.rej", "foo-bar.rej"])
