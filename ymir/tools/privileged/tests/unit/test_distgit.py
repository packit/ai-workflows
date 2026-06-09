import git
import pytest
from beeai_framework.tools import ToolError
from flexmock import flexmock
from specfile.utils import EVR

from ymir.tools.privileged import distgit as distgit_tools
from ymir.tools.privileged.distgit import (
    CreateZstreamBranchTool,
    _is_transient_git_error,
    _retry_transient,
)


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
                .and_return([])
                .mock(),
            ),
        ),
    )

    async def mock_get_latest_candidate_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el10"), ref

    flexmock(distgit_tools).should_receive("get_latest_candidate_build").replace_with(
        mock_get_latest_candidate_build
    ).times(0 if branch_exists else 1)

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


@pytest.mark.asyncio
async def test_create_zstream_branch_push_rejected(monkeypatch):
    """Push is silently rejected by gitolite — ToolError must be raised immediately."""
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"
    ref = "123456abcdef"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    mock_push_info = flexmock(flags=git.remote.PushInfo.ERROR, summary="access denied")
    flexmock(git.Repo).should_receive("clone_from").and_return(
        flexmock(
            git=gitcmd,
            remotes=flexmock(
                origin=flexmock(refs=[])
                .should_receive("push")
                .with_args(f"{ref}:refs/heads/{branch}")
                .once()
                .and_return([mock_push_info])
                .mock(),
            ),
        ),
    )

    async def mock_get_latest_candidate_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el10"), ref

    flexmock(distgit_tools).should_receive("get_latest_candidate_build").replace_with(
        mock_get_latest_candidate_build
    ).once()

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    with pytest.raises(ToolError, match="Push rejected"):
        await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})


@pytest.mark.parametrize(
    "stderr, expected_transient",
    [
        ("Connection closed by 10.2.32.39 port 22\nfatal: Could not read from remote repository.", True),
        ("Connection reset by peer", True),
        ("ssh_exchange_identification: Connection closed by remote host", True),
        ("error: failed to push some refs to 'ssh://pkgs.devel.redhat.com/rpms/ruby'", True),
        ("Permission denied (publickey)", False),
        ("fatal: Authentication failed for 'https://example.com/'", False),
        ("fatal: Could not read from remote repository.", False),
    ],
)
def test_is_transient_git_error(stderr, expected_transient):
    exc = git.exc.GitCommandError(["git", "clone"], status=128, stderr=stderr)
    assert _is_transient_git_error(exc) == expected_transient


@pytest.mark.asyncio
async def test_retry_transient_clone_recovers():
    """Clone fails transiently twice then succeeds on third attempt."""
    call_count = 0

    async def flaky_clone():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise git.exc.GitCommandError(
                ["git", "clone"], status=128, stderr="Connection closed by 10.2.32.39 port 22"
            )
        return "cloned"

    result = await _retry_transient(flaky_clone, "test-clone", max_retries=3, base_delay=0)
    assert result == "cloned"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_transient_permanent_error_no_retry():
    """Permanent errors are raised immediately without retrying."""
    call_count = 0

    async def permanent_fail():
        nonlocal call_count
        call_count += 1
        raise git.exc.GitCommandError(["git", "clone"], status=128, stderr="Permission denied (publickey)")

    with pytest.raises(git.exc.GitCommandError, match="Permission denied"):
        await _retry_transient(permanent_fail, "test-clone", max_retries=3, base_delay=0)
    assert call_count == 1
