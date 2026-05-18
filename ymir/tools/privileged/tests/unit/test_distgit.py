import git
import koji
import pytest
from flexmock import flexmock

from ymir.tools.privileged import distgit as distgit_tools
from ymir.tools.privileged.distgit import CreateZstreamBranchTool


@pytest.mark.parametrize(
    "branch_exists",
    [False, True],
)
@pytest.mark.asyncio
async def test_create_zstream_branch(branch_exists, monkeypatch):
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"
    ref = "123456abcdef"  # pragma: allowlist secret

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

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result
    if branch_exists:
        assert "already exists" in result
    else:
        assert result.startswith("Successfully")


@pytest.mark.asyncio
async def test_create_zstream_branch_distgit_has_branch(monkeypatch):
    """Branch already in dist-git but not yet mirrored to GitLab (retry after partial push success)."""
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    # GitLab check: branch not present yet
    gitcmd = flexmock().should_receive("ls_remote").and_return(False).and_return(True).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    mock_ref = flexmock(name=f"origin/{branch}")
    flexmock(git.Repo).should_receive("clone_from").and_return(
        flexmock(
            git=gitcmd,
            remotes=flexmock(
                origin=flexmock(refs=[mock_ref])
                .should_receive("push")
                .times(0)  # no push — branch is already in dist-git
                .mock(),
            ),
        ),
    )

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result
    assert result.startswith("Successfully")
