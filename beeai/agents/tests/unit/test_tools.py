import datetime
import subprocess
from textwrap import dedent

import pytest
from flexmock import flexmock
from specfile import specfile

from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware

from tools.wicked_git import (
    GitPatchCreationTool,
    GitPatchCreationToolInput,
    GitLogSearchTool,
    GitLogSearchToolInput,
)
from tools.commands import RunShellCommandTool, RunShellCommandToolInput
from tools.specfile import (
    AddChangelogEntryTool,
    AddChangelogEntryToolInput,
    BumpReleaseTool,
    BumpReleaseToolInput,
    SetZStreamReleaseTool,
    SetZStreamReleaseToolInput,
)
from tools.text import (
    CreateTool,
    CreateToolInput,
    ViewTool,
    ViewToolInput,
    InsertTool,
    InsertToolInput,
    StrReplaceTool,
    StrReplaceToolInput,
)


@pytest.mark.parametrize(
    "command, exit_code, stdout, stderr",
    [
        (
            "exit 28",
            28,
            None,
            None,
        ),
        (
            "echo -n test",
            0,
            "test",
            None,
        ),
        (
            "echo -n error >&2 && false",
            1,
            None,
            "error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_shell_command(command, exit_code, stdout, stderr):
    tool = RunShellCommandTool()
    output = await tool.run(input=RunShellCommandToolInput(command=command)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.to_json_safe()
    assert result.exit_code == exit_code
    assert result.stdout == stdout
    assert result.stderr == stderr


@pytest.fixture
def minimal_spec(tmp_path):
    spec = tmp_path / "test.spec"
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        0.1
            Release:        2%{?dist}
            Summary:        Test package

            License:        MIT

            %description
            Test package

            %changelog
            * Thu Jun 07 2018 Nikola Forró <nforro@redhat.com> - 0.1-1
            - first version
            """
        )
    )
    return spec


@pytest.mark.asyncio
async def test_add_changelog_entry(minimal_spec):
    content = ["- some change", "  second line"]
    flexmock(specfile).should_receive("guess_packager").and_return(f"RHEL Packaging Agent <jotnar@redhat.com>")
    flexmock(specfile).should_receive("datetime").and_return(
        flexmock(
            datetime=flexmock(now=lambda _: flexmock(date=lambda: datetime.date(2025, 8, 5))),
            timezone=flexmock(utc=None),
        )
    )
    tool = AddChangelogEntryTool()
    output = await tool.run(
        input=AddChangelogEntryToolInput(spec=minimal_spec, content=content)
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    assert minimal_spec.read_text().splitlines()[-7:-2] == [
        "%changelog",
        "* Tue Aug 05 2025 RHEL Packaging Agent <jotnar@redhat.com> - 0.1-2",
        "- some change",
        "  second line",
        "",
    ]


@pytest.mark.asyncio
async def test_bump_release(minimal_spec):
    tool = BumpReleaseTool()
    output = await tool.run(input=BumpReleaseToolInput(spec=minimal_spec)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result.startswith("Successfully")
    assert minimal_spec.read_text().splitlines()[3] == "Release:        3%{?dist}"


@pytest.fixture
def autorelease_spec(tmp_path):
    spec = tmp_path / "test.spec"
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        0.1
            Release:        %autorelease
            Summary:        Test package

            License:        MIT

            %description
            Test package

            %changelog
            %autochangelog
            """
        )
    )
    return spec


@pytest.mark.asyncio
async def test_set_zstream_release(autorelease_spec):
    latest_ystream_evr = "0.1-4.el10"
    tool = SetZStreamReleaseTool()
    output = await tool.run(
        input=SetZStreamReleaseToolInput(spec=autorelease_spec, latest_ystream_evr=latest_ystream_evr)
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    assert autorelease_spec.read_text().splitlines()[3] == "Release:        4%{?dist}.%{autorelease -n}"


@pytest.mark.asyncio
async def test_create(tmp_path):
    test_file = tmp_path / "test.txt"
    content = "Line 1\nLine 2\n"
    tool = CreateTool()
    output = await tool.run(input=CreateToolInput(file=test_file, content=content)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result.startswith("Successfully")
    assert test_file.read_text() == content


@pytest.mark.asyncio
async def test_view(tmp_path):
    test_dir = tmp_path
    test_file = test_dir / "test.txt"
    content = "Line 1\nLine 2\nLine 3\n"
    test_file.write_text(content)
    tool = ViewTool()
    output = await tool.run(input=ViewToolInput(path=test_dir)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result == "test.txt\n"
    output = await tool.run(input=ViewToolInput(path=test_file)).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert result == content
    output = await tool.run(input=ViewToolInput(path=test_file, view_range=[2, -1])).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert (
        result
        == dedent(
            """
            Line 2
            Line 3
            """
        )[1:]
    )
    output = await tool.run(input=ViewToolInput(path=test_file, view_range=[1, 2])).middleware(
        GlobalTrajectoryMiddleware(pretty=True)
    )
    result = output.result
    assert (
        result
        == dedent(
            """
            Line 1
            Line 2
            """
        )[1:]
    )


@pytest.mark.parametrize(
    "line, content",
    [
        (
            0,
            dedent(
                """
                Inserted line
                Line 1
                Line 2
                Line 3
                """
            )[1:],
        ),
        (
            1,
            dedent(
                """
                Line 1
                Inserted line
                Line 2
                Line 3
                """
            )[1:],
        ),
    ],
)
@pytest.mark.asyncio
async def test_insert(line, content, tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = InsertTool()
    output = await tool.run(
        input=InsertToolInput(file=test_file, line=line, new_string="Inserted line")
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    assert test_file.read_text() == content


@pytest.mark.asyncio
async def test_str_replace(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = StrReplaceTool()
    output = await tool.run(
        input=StrReplaceToolInput(file=test_file, old_string="Line 2", new_string="LINE_2")
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    assert (
        test_file.read_text()
        == dedent(
            """
            Line 1
            LINE_2
            Line 3
            """
        )[1:]
    )

@pytest.mark.asyncio
async def test_git_patch_creation_tool_nonexistent_repo(tmp_path):
    # This test checks the error message for a non-existent repo path
    repo_path = tmp_path / "not_a_repo"
    patch_file_path = tmp_path / "patch.patch"
    tool = GitPatchCreationTool()
    output = await tool.run(
        input=GitPatchCreationToolInput(
            repository_path=str(repo_path),
            patch_file_path=str(patch_file_path),
        )
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert "ERROR: Repository path does not exist" in result

@pytest.fixture
def git_repo(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=repo_path, check=True)
    # Create a file and commit it
    file_path = repo_path / "file.txt"
    file_path.write_text("Line 1\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit\n\nCVE-2025-12345"],
        cwd=repo_path, check=True)
    file_path.write_text("Line 1\nLine 2\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit2\n\nResolves: RHEL-123456"],
        cwd=repo_path, check=True)
    subprocess.run(["git", "branch", "line-2"], cwd=repo_path, check=True)
    return repo_path

@pytest.mark.asyncio
async def test_git_patch_creation_tool_success(git_repo, tmp_path):
    # Simulate a git-am session by creating a new commit and then using format-patch
    # Create a new file and stage it
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=git_repo, check=True)
    new_file = git_repo / "file.txt"
    new_file.write_text("Line 1\nLine 3\n")
    subprocess.run(["git", "add", "file.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "Add line 3"], cwd=git_repo, check=True)

    patch_file = tmp_path / "patch.patch"
    subprocess.run(["git", "format-patch", "-1", "HEAD", "--stdout"], cwd=git_repo, check=True, stdout=patch_file.open("w"))

    subprocess.run(["git", "switch", "line-2"], cwd=git_repo, check=True)

    # Now apply the patch with git am
    # This will fail with a merge conflict, but we don't care about that
    subprocess.run(["git", "am", str(patch_file)], cwd=git_repo)

    new_file.write_text("Line 1\nLine 2\nLine 3\n")

    # Now use the tool to create a patch file from the repo
    tool = GitPatchCreationTool()
    output_patch = tmp_path / "output.patch"
    output = await tool.run(
        input=GitPatchCreationToolInput(
            repository_path=str(git_repo),
            patch_file_path=str(output_patch),
        )
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert "Successfully created a patch file" in result
    assert output_patch.exists()
    # The patch file should contain the commit message "Add line 3"
    assert "Add line 3" in output_patch.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cve_id, jira_issue, expected",
    [
        ("CVE-2025-12345", "", "Found 1 matching commit(s) for 'CVE-2025-12345'"),
        ("CVE-2025-12346", "", "No matches found for 'CVE-2025-12346'"),
        ("", "RHEL-123456", "Found 1 matching commit(s) for 'RHEL-123456'"),
        ("", "RHEL-123457", "No matches found for 'RHEL-123457'"),
    ]
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
