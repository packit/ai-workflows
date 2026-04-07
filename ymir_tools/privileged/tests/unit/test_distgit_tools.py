import asyncio
import pytest
from pathlib import Path

import git
import koji
from flexmock import flexmock

import distgit_tools
from distgit_tools import create_zstream_branch


@pytest.mark.parametrize(
    "branch_exists",
    [False, True],
)
@pytest.mark.asyncio
async def test_create_zstream_branch(branch_exists, monkeypatch):
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"
    ref = "123456abcdef"

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(branch_exists).and_return(True).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    flexmock(git.Repo).should_receive("clone_from").and_return(
        flexmock(
            git=gitcmd,
            remotes=flexmock(
                origin=flexmock(refs=[])
                .should_receive("push")
                .with_args(f"{ref}:refs/heads/{branch}")
                .times(0 if branch_exists else 1)
                .mock(),
            ),
        ),
    )

    flexmock(koji.ClientSession).new_instances(
        flexmock(
            listTagged=lambda *_, **__: [{"build_id": 12345}],
            getBuild=lambda *_, **__: {"source": f"some_git_url#{ref}"},
        ),
    )

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = await create_zstream_branch(package=package, branch=branch)
    if branch_exists:
        assert "already exists" in result
    else:
        assert result.startswith("Successfully")
