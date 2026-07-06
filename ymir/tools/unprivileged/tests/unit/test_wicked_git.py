import subprocess

import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError
from flexmock import flexmock

from ymir.tools.unprivileged import wicked_git as wicked_git_mod
from ymir.tools.unprivileged.wicked_git import (
    BuildSrpmInput,
    BuildSrpmTool,
    GitLogSearchTool,
    GitLogSearchToolInput,
    GitPatchApplyFinishTool,
    GitPatchApplyFinishToolInput,
    GitPatchApplyTool,
    GitPatchApplyToolInput,
    GitPatchCreationTool,
    GitPatchCreationToolInput,
    RunPackagePrepInput,
    RunPackagePrepTool,
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
        ("CVE-2025-12345", "", "CVE-2025-12345: found"),
        ("CVE-2025-12346", "", "CVE-2025-12346: not found"),
        ("", "RHEL-123456", "RHEL-123456: found"),
        ("", "rhel-123456", "rhel-123456: found"),
        ("", "RHEL-123457", "RHEL-123457: not found"),
        (
            "CVE-2025-12345 CVE-2025-99999",
            "",
            "CVE-2025-12345: found\nCVE-2025-99999: not found",
        ),
        (
            "CVE-2025-99998, CVE-2025-99999",
            "",
            "CVE-2025-99998: not found\nCVE-2025-99999: not found",
        ),
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
        (
            "diff --git a/t/new-test.t b/t/new-test.t\n"
            "new file mode 100644\n"
            "index 0000000..cb75215\n"
            "--- /dev/null\n"
            "+++ b/t/new-test.t\n"
            "@@ -0,0 +1,2 @@\n"
            "+use Test::More;\n"
            "+ok(1);\n",
            1,
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


@pytest.fixture
def dist_git_dir(tmp_path):
    """Simulate a dist-git directory with a spec file."""
    dist_git = tmp_path / "dist-git"
    dist_git.mkdir()
    (dist_git / "ruby.spec").write_text("Name: ruby\nVersion: 3.3.10\n")
    return dist_git


@pytest.mark.asyncio
async def test_run_package_prep_success(dist_git_dir):
    async def mock_run_subprocess(cmd, **kwargs):
        return (0, "prep done successfully", "")

    flexmock(wicked_git_mod).should_receive("run_subprocess").replace_with(mock_run_subprocess).once()

    tool = RunPackagePrepTool()
    output = await tool.run(
        input=RunPackagePrepInput(
            dist_git_path=str(dist_git_dir),
            package="ruby",
            dist_git_branch="c10s",
        ),
    )
    assert "Prep succeeded" in output.result
    assert "prep done successfully" in output.result


@pytest.mark.asyncio
async def test_run_package_prep_failure_cleans_build_dir(dist_git_dir):
    # Simulate a partially-patched build subdirectory left behind by rpmbuild
    build_dir = dist_git_dir / "ruby-3.3.10"
    build_dir.mkdir()
    (build_dir / "patched_file.rb").write_text("partially applied content")

    async def mock_run_subprocess(cmd, **kwargs):
        return (1, "", "patch failed to apply")

    flexmock(wicked_git_mod).should_receive("run_subprocess").replace_with(mock_run_subprocess).once()

    tool = RunPackagePrepTool()
    output = await tool.run(
        input=RunPackagePrepInput(
            dist_git_path=str(dist_git_dir),
            package="ruby",
            dist_git_branch="c10s",
        ),
    )
    assert "Prep FAILED" in output.result
    assert "cleaned up" in output.result
    assert not build_dir.exists(), "Build directory should have been removed on failure"


@pytest.mark.asyncio
async def test_run_package_prep_failure_preserves_non_matching_dirs(dist_git_dir):
    # Create directories: one matching the package name, one not
    build_dir = dist_git_dir / "ruby-3.3.10"
    build_dir.mkdir()
    other_dir = dist_git_dir / "some-other-dir"
    other_dir.mkdir()

    async def mock_run_subprocess(cmd, **kwargs):
        return (1, "", "")

    flexmock(wicked_git_mod).should_receive("run_subprocess").replace_with(mock_run_subprocess).once()

    tool = RunPackagePrepTool()
    await tool.run(
        input=RunPackagePrepInput(
            dist_git_path=str(dist_git_dir),
            package="ruby",
            dist_git_branch="c10s",
        ),
    )
    assert not build_dir.exists(), "Build directory should have been removed"
    assert other_dir.exists(), "Non-matching directory should be preserved"


@pytest.mark.asyncio
async def test_run_package_prep_nonexistent_path(tmp_path):
    tool = RunPackagePrepTool()
    with pytest.raises(ToolError) as e:
        await tool.run(
            input=RunPackagePrepInput(
                dist_git_path=str(tmp_path / "nonexistent"),
                package="ruby",
                dist_git_branch="c10s",
            ),
        )
    assert "does not exist" in e.value.message


@pytest.mark.asyncio
async def test_build_srpm_success(dist_git_dir):
    srpm_path = "/tmp/dist-git/ruby-3.3.10-1.el10.src.rpm"

    async def mock_run_subprocess(cmd, **kwargs):
        return (0, f"Wrote: {srpm_path}\n", "")

    flexmock(wicked_git_mod).should_receive("run_subprocess").replace_with(mock_run_subprocess).once()

    tool = BuildSrpmTool()
    output = await tool.run(
        input=BuildSrpmInput(
            dist_git_path=str(dist_git_dir),
            package="ruby",
            dist_git_branch="c10s",
        ),
    )
    assert output.result == srpm_path


@pytest.mark.asyncio
async def test_build_srpm_failure(dist_git_dir):
    async def mock_run_subprocess(cmd, **kwargs):
        return (1, "", "error: Bad source")

    flexmock(wicked_git_mod).should_receive("run_subprocess").replace_with(mock_run_subprocess).once()

    tool = BuildSrpmTool()
    output = await tool.run(
        input=BuildSrpmInput(
            dist_git_path=str(dist_git_dir),
            package="ruby",
            dist_git_branch="c10s",
        ),
    )
    assert "SRPM build FAILED" in output.result
    assert "Bad source" in output.result


@pytest.mark.asyncio
async def test_build_srpm_nonexistent_path(tmp_path):
    tool = BuildSrpmTool()
    with pytest.raises(ToolError) as e:
        await tool.run(
            input=BuildSrpmInput(
                dist_git_path=str(tmp_path / "nonexistent"),
                package="ruby",
                dist_git_branch="c10s",
            ),
        )
    assert "does not exist" in e.value.message
