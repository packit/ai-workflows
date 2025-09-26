import contextlib
import datetime
import subprocess
from textwrap import dedent

import pytest
from flexmock import flexmock
from specfile import specfile
from specfile.utils import EVR

from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import ToolError

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
    UpdateReleaseTool,
    UpdateReleaseToolInput,
)
from tools.text import (
    CreateTool,
    CreateToolInput,
    InsertAfterSubstringTool,
    InsertAfterSubstringToolInput,
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
    spec = tmp_path / "minimal.spec"
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


@pytest.fixture
def autorelease_spec(tmp_path):
    spec = tmp_path / "autorelease.spec"
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


@pytest.mark.parametrize(
    "rebase",
    [False, True],
)
@pytest.mark.parametrize(
    "dist_git_branch, ystream_dist",
    [
        ("c9s", ".el9"),
        ("c10s", ".el10"),
        ("rhel-9.6.0", ".el9"),
        ("rhel-10.0", ".el10"),
    ],
)
@pytest.mark.asyncio
async def test_update_release(rebase, dist_git_branch, ystream_dist, minimal_spec, autorelease_spec):
    package = "test"

    async def _get_latest_ystream_build(*_, **__):
        return EVR(version="0.1", release="2" + ystream_dist)

    flexmock(UpdateReleaseTool).should_receive("_get_latest_ystream_build").replace_with(_get_latest_ystream_build)

    tool = UpdateReleaseTool()

    async def run_and_check(spec, expected_release, error=False):
        with (pytest.raises(ToolError) if error else contextlib.nullcontext()) as e:
            output = await tool.run(
                input=UpdateReleaseToolInput(
                    spec=spec, package=package, dist_git_branch=dist_git_branch, rebase=rebase
                )
            ).middleware(GlobalTrajectoryMiddleware(pretty=True))
        if error:
            return e.value.message
        result = output.result
        assert result.startswith("Successfully")
        assert spec.read_text().splitlines()[3] == f"Release:        {expected_release}"
        return result

    if not dist_git_branch.startswith("rhel-"):
        await run_and_check(minimal_spec, "1%{?dist}" if rebase else "3%{?dist}")
        await run_and_check(autorelease_spec, "%autorelease")
    else:
        await run_and_check(minimal_spec, "0%{?dist}.1" if rebase else "2%{?dist}.1")
        await run_and_check(autorelease_spec, "0%{?dist}.%{autorelease -n}" if rebase else "2%{?dist}.%{autorelease -n}")
        await run_and_check(minimal_spec, "0%{?dist}.1" if rebase else "2%{?dist}.2")
        await run_and_check(autorelease_spec, "0%{?dist}.%{autorelease -n}" if rebase else "2%{?dist}.%{autorelease -n}")
        if not rebase:
            minimal_spec.write_text(minimal_spec.read_text().replace("%{?dist}.2", "%{?dist}.1.0.0.hotfix2.rhel12345"))
            assert (await run_and_check(minimal_spec, None, error=True)).endswith("Unable to determine valid release")


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


@pytest.mark.parametrize(
    "insert_after_substring, final_content",
    [
        (
            "Line 1",
            "Line 1\nInserted line\nLine 2\nLine 3\n",
        ),
        (
            "Line 2",
            "Line 1\nLine 2\nInserted line\nLine 3\n",
        ),
        (
            "Line 3",
            "Line 1\nLine 2\nLine 3\nInserted line\n",
        ),
    ],
)
@pytest.mark.asyncio
async def test_insert_after_substring(insert_after_substring, final_content, tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = InsertAfterSubstringTool()
    output = await tool.run(
        input=InsertAfterSubstringToolInput(file=test_file, insert_after_substring=insert_after_substring, new_string="Inserted line")
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.result
    assert result.startswith("Successfully")
    assert test_file.read_text() == final_content


@pytest.mark.asyncio
async def test_insert_after_substring_missing(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")
    tool = InsertAfterSubstringTool()
    with pytest.raises(ToolError) as e:
        await tool.run(
            input=InsertAfterSubstringToolInput(file=test_file, insert_after_substring="Line 4", new_string="Inserted line")
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = e.value.message
    assert "No insertion was done because the specified substring wasn't present" in result


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
    with pytest.raises(ToolError) as e:
        await tool.run(
            input=GitPatchCreationToolInput(
                repository_path=str(repo_path),
                patch_file_path=str(patch_file_path),
            )
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = e.value.message
    assert "Repository path does not exist" in result

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
    tool = GitPatchCreationTool(options={"this_cannot_be_empty": "sure-why-not"})
    tool.options["base_head_commit"] = subprocess.run(["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True).stdout.strip()
    output_patch = tmp_path / "output.patch"
    # got inspired by bee's MCPTool test case:
    #  https://github.com/i-am-bee/beeai-framework/blob/main/python/tests/tools/test_mcp_tool.py
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
    """ Verifies that GitPatchCreationTool can recover from a `git am` failure
    caused by a patch file without a proper header (i.e., missing author identity).
    """
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
    # Now apply the patch with git am
    proc = subprocess.run(["git", "am", str(patch_file)], cwd=git_repo, capture_output=True, text=True)
    assert proc.returncode != 0, "git am was expected to fail but succeeded"
    # verify the git-am fails with the expected error message
    assert "fatal: empty ident name (for <>) not allowed" in proc.stderr

    # Now use the tool to create a patch file from the repo
    tool = GitPatchCreationTool(options={"this_cannot_be_empty": "sure-why-not"})
    tool.options["base_head_commit"] = subprocess.run(["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True).stdout.strip()
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
